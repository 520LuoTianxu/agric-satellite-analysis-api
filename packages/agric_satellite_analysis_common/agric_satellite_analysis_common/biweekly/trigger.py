"""Explicitly triggered biweekly jobs. Nothing is auto-published.

Local fixture mode only: plans bounded backfill and builds snapshots from the
6417 pack. It never dispatches production satellite_batch, never writes a
business database, and never scans lands on a timer.
"""

from __future__ import annotations

import json
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .calendar import bounded_backfill_window
from .catalog import ALLOWED_REFERENCE_IDS, LocalPackCatalog
from .models import ReportError, canonical, digest
from .phase_policy import (
    FORBIDDEN_REPORT_METRICS,
    POLICY_VERSION,
    compare_to_prior,
    display_phase,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
STATES = ("queued", "running", "ready", "failed", "evidence_limited")


def _repo_root() -> Path:
    # .../packages/agric_satellite_analysis_common/agric_satellite_analysis_common/biweekly/trigger.py
    return Path(__file__).resolve().parents[4]


def reference_fixture_path() -> Path:
    return _repo_root() / "examples" / "biweekly" / "reference_plan_15933.json"


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


class TriggerService:
    """In-process, file-backed job store for the local trigger API."""

    def __init__(self, work_dir: Path, catalog: LocalPackCatalog | None = None, *, auto_run=True):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        (self.work_dir / "jobs").mkdir(exist_ok=True)
        (self.work_dir / "artifacts").mkdir(exist_ok=True)
        self.catalog = catalog or LocalPackCatalog()
        self.auto_run = auto_run

    def list_seasons(self, land_id: str) -> dict:
        seasons = self.catalog.list_seasons(land_id)
        return {
            "land_id": str(land_id),
            "seasons": seasons,
            "auto_publish": False,
            "note": "默认不出报。列出种植季后需显式触发补数或生成。",
        }

    def list_periods(self, land_id: str, season_id: str) -> dict:
        periods = []
        for row in self.catalog.periods(land_id, season_id):
            latest = self._latest_for_period(land_id, season_id, row["period_id"])
            periods.append({**row, "latest_job": latest})
        return {
            "land_id": str(land_id),
            "season_id": season_id,
            "auto_publish": False,
            "periods": periods,
        }

    def get_job(self, job_id: str) -> dict:
        path = self.work_dir / "jobs" / f"{job_id}.json"
        if not path.is_file():
            raise ReportError("JOB_NOT_FOUND")
        return json.loads(path.read_text())

    def download(self, job_id: str) -> tuple[str, bytes, str]:
        job = self.get_job(job_id)
        if job["status"] not in {"ready", "evidence_limited"}:
            raise ReportError("ARTIFACT_UNAVAILABLE")
        artifact = job.get("artifact") or {}
        rel = artifact.get("path")
        if not rel:
            raise ReportError("ARTIFACT_UNAVAILABLE")
        path = self.work_dir / rel
        data = path.read_bytes()
        if digest(data) != artifact.get("sha256"):
            raise ReportError("ARTIFACT_CORRUPT")
        return artifact.get("filename") or path.name, data, artifact.get("mime") or "application/pdf"

    def request_backfill(self, land_id: str, season_id: str, period_id: str) -> dict:
        period = self.catalog.period(land_id, season_id, period_id)
        periods = self.catalog.periods(land_id, season_id)
        window = bounded_backfill_window(periods, period["period_id"])
        planned = {
            "task_type": "satellite_batch",
            "dispatch": "plan_only_local_fixture",
            "land_id_list": [str(land_id)],
            "date_from": window["date_from"],
            "date_to": window["date_to_inclusive"],
            "sensors": ["S2", "S1"],
            "force": False,
            "years": None,
            "months": None,
            "window": window,
        }
        job = self._create(
            kind="backfill",
            land_id=land_id,
            season_id=season_id,
            period=period,
            payload={"planned_batch": planned},
        )
        if self.auto_run:
            job = self.run_job(job["job_id"])
        return job

    def request_generate(
        self,
        land_id: str,
        season_id: str,
        period_id: str,
        *,
        reference_planting_instance_id: str | None = None,
        observations_override: list | None = None,
        prior_observations_override: list | None = None,
        native_plan: dict | None = None,
        observation_source: str | None = None,
    ) -> dict:
        period = self.catalog.period(land_id, season_id, period_id)
        reference = self._load_reference(reference_planting_instance_id)
        job = self._create(
            kind="generate",
            land_id=land_id,
            season_id=season_id,
            period=period,
            payload={
                "reference_planting_instance_id": reference["planting_instance_id"]
                if reference
                else None,
                "reference_disclosure": reference["disclosure"] if reference else None,
                "observations_override": observations_override,
                "prior_observations_override": prior_observations_override,
                "native_plan": native_plan,
                "observation_source": observation_source,
            },
        )
        if self.auto_run:
            job = self.run_job(job["job_id"])
        return job

    def run_job(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if job["status"] not in {"queued", "running"}:
            return job
        self._transition(job, "running")
        try:
            if job["kind"] == "backfill":
                result = self._run_backfill(job)
                self._transition(job, "ready", result=result)
            elif job["kind"] == "generate":
                result, artifact, status = self._run_generate(job)
                self._transition(job, status, result=result, artifact=artifact)
            else:
                raise ReportError("JOB_KIND_UNSUPPORTED")
        except ReportError as exc:
            self._transition(job, "failed", error_code=exc.code)
        return self.get_job(job_id)

    def _create(self, *, kind, land_id, season_id, period, payload) -> dict:
        job_id = "bj-" + secrets.token_hex(8)
        job = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "land_id": str(land_id),
            "season_id": season_id,
            "period_id": period["period_id"],
            "window": {
                "start": period["start"],
                "end_inclusive": period["end_inclusive"],
                "end_exclusive": period["end_exclusive"],
            },
            "auto_publish": False,
            "created_at": _now(),
            "updated_at": _now(),
            "history": [{"status": "queued", "at": _now()}],
            "payload": payload,
            "result": None,
            "artifact": None,
            "error_code": None,
            "policy_version": POLICY_VERSION,
        }
        self._write(job)
        return job

    def _write(self, job: dict) -> None:
        path = self.work_dir / "jobs" / f"{job['job_id']}.json"
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")

    def _transition(self, job, status, result=None, artifact=None, error_code=None):
        if status not in STATES:
            raise ReportError("STATE_INVALID")
        job["status"] = status
        job["updated_at"] = _now()
        job["history"].append({"status": status, "at": job["updated_at"]})
        if result is not None:
            job["result"] = result
        if artifact is not None:
            job["artifact"] = artifact
        if error_code is not None:
            job["error_code"] = error_code
        self._write(job)

    def _latest_for_period(self, land_id, season_id, period_id):
        latest = None
        for path in sorted((self.work_dir / "jobs").glob("bj-*.json")):
            job = json.loads(path.read_text())
            if (
                job.get("land_id") == str(land_id)
                and job.get("season_id") == season_id
                and job.get("period_id") == str(period_id)
                and job.get("kind") == "generate"
            ):
                latest = {
                    "job_id": job["job_id"],
                    "status": job["status"],
                    "created_at": job["created_at"],
                    "reference": bool(job.get("payload", {}).get("reference_planting_instance_id")),
                }
        return latest

    def _load_reference(self, reference_id: str | None) -> dict | None:
        if reference_id in (None, ""):
            return None
        key = str(reference_id)
        if key not in ALLOWED_REFERENCE_IDS:
            raise ReportError("UNKNOWN_REFERENCE")
        path = reference_fixture_path()
        if not path.is_file():
            raise ReportError("REFERENCE_FIXTURE_MISSING")
        data = json.loads(path.read_text())
        if str(data.get("planting_instance_id")) != key:
            raise ReportError("REFERENCE_FIXTURE_MISSING")
        return data

    def _run_backfill(self, job: dict) -> dict:
        planned = job["payload"]["planned_batch"]
        span = planned["window"]["span_days"]
        if span >= 365 or planned.get("years") or planned.get("months"):
            raise ReportError("BACKFILL_WINDOW_UNBOUNDED")
        period = self.catalog.period(job["land_id"], job["season_id"], job["period_id"])
        snapshot = self.catalog.load_snapshot(period["snapshot"])
        optical = [
            row
            for row in snapshot.get("observations") or []
            if row.get("sensor") == "S2" and row.get("quality_status") == "eligible_descriptive"
        ]
        return {
            "mode": "local_fixture_evaluation",
            "dispatched_to_production": False,
            "planned_batch": planned,
            "eligible_optical_days": len({row["date"][:10] for row in optical}),
            "s1_in_snapshot": int(period.get("s1_products") or 0),
            "scenes_found": bool(optical) or int(period.get("s1_products") or 0) > 0,
            "eligible_optics_found": bool(optical),
            "note": "补数完成不会自动出报。若仍无可用光学，再显式调用生成，将得到 evidence_limited 简报。",
        }

    def _run_generate(self, job: dict) -> tuple[dict, dict, str]:
        period = self.catalog.period(job["land_id"], job["season_id"], job["period_id"])
        snapshot = {}
        if period.get("snapshot"):
            try:
                snapshot = self.catalog.load_snapshot(period["snapshot"]) or {}
            except ReportError:
                snapshot = {}
        override = job.get("payload", {}).get("observations_override")
        if override is not None:
            source_rows = override
            cutoff = period["end_exclusive"] + "T00:00:00+08:00"
        else:
            source_rows = snapshot.get("observations") or []
            cutoff = snapshot.get("observation_cutoff") or (period["end_exclusive"] + "T00:00:00+08:00")
        if not cutoff.endswith("+08:00") and "+08" not in cutoff:
            raise ReportError("CUTOFF_TIMEZONE_INVALID")
        observations = []
        for row in source_rows:
            day = str(row.get("date"))[:10]
            if day >= period["end_exclusive"] or day < period["start"]:
                continue
            if row.get("sensor") != "S2":
                continue
            if row.get("quality_status") != "eligible_descriptive":
                continue
            observations.append(row)

        enriched = self._attach_v8_metrics(period, observations)
        reference = None
        ref_id = job["payload"].get("reference_planting_instance_id")
        if ref_id:
            reference = self._load_reference(ref_id)

        native = job.get("payload", {}).get("native_plan") or None
        if native and native.get("intervals"):
            phase = display_phase(
                planned_intervals=native["intervals"],
                observation_dates=[row["date"][:10] for row in enriched],
                window_start=period["start"],
                end_exclusive=period["end_exclusive"],
            )
            phase["phenology_basis"] = native.get("phenology_basis") or "PLAN_DERIVED_FROM_SMART_PLANT_TASK"
            phase["plan_source"] = "native"
            phase["plan_label"] = "本田方案"
        elif reference:
            phase = display_phase(
                planned_intervals=reference["intervals"],
                observation_dates=[row["date"][:10] for row in enriched],
                window_start=period["start"],
                end_exclusive=period["end_exclusive"],
            )
            phase["phenology_basis"] = "PLAN_DERIVED_FROM_BTU_REFERENCE"
            phase["plan_source"] = "reference"
            phase["reference"] = {
                "planting_instance_id": reference["planting_instance_id"],
                "name": reference["name"],
                "disclosure": reference["disclosure"],
                "coverage": reference["coverage"],
            }
        else:
            phase = {
                "phenology_basis": "none",
                "display_phase": None,
                "primary_metrics": [],
                "supporting_metrics": [],
                "note": "本田未关联种植方案。未传入参照实例，因此报告不给出生育期或三阶段判断。",
            }

        prior_cmp = None
        metrics_for_compare = list(phase.get("primary_metrics") or []) + list(
            phase.get("supporting_metrics") or []
        )
        if not metrics_for_compare:
            metrics_for_compare = ["ndvi", "evi", "ndre", "ndmi"]
        metrics_for_compare = [m for m in metrics_for_compare if m not in FORBIDDEN_REPORT_METRICS]
        prior_rows = self._prior_optics(job, period)
        if enriched and prior_rows:
            prior_cmp = compare_to_prior(enriched, prior_rows, metrics_for_compare)

        facts = {
            "schema_version": "biweekly-trigger-snapshot-v1",
            "land_id": job["land_id"],
            "season_id": job["season_id"],
            "period": period,
            "policy_version": POLICY_VERSION,
            "cutoff": cutoff,
            "optical_days": [row["date"][:10] for row in enriched],
            "phase": phase,
            "prior_comparison": prior_cmp,
            "weather_summary": snapshot.get("weather_summary"),
            "science_limits": snapshot.get("science_limits"),
            "forbidden_metrics_omitted": sorted(FORBIDDEN_REPORT_METRICS),
            "generated_at": _now(),
            "observation_source": job.get("payload", {}).get("observation_source") or ("pack_snapshot" if snapshot else "none"),
        }
        if enriched and len(enriched) < 2 and prior_cmp:
            facts["quality_note"] = "仅1次有效观测，不能写成持续改善。"

        if not enriched:
            facts["brief"] = "本期没有可用光学观测。此为证据受限简报，不作出长势好转或阶段诊断。"
            if (period.get("s1_products") or 0) == 0:
                facts["radar_note"] = "本期没有雷达影像，不对内涝风险作出偏低或偏高的结论。"
            status = "evidence_limited"
            artifact = self._write_brief(job["job_id"], facts, period)
        else:
            status = "ready"
            artifact = self._write_ready_artifact(job["job_id"], facts, period, reference is not None)
        facts["content_sha256"] = digest(canonical(facts))
        return facts, artifact, status

    def _attach_v8_metrics(self, period, observations):
        optical = self.catalog.v8_optical_metrics() or {}
        dates = (optical.get("dates") or {}) if period["start"] == "2026-04-04" else {}
        rows = []
        for row in observations:
            item = dict(row)
            day = item["date"][:10]
            extra = dates.get(day) or {}
            for metric in ("ndvi", "evi", "ndre", "ndmi"):
                key = f"{metric}_avg"
                if extra.get(key) is not None:
                    item[key] = extra[key]
                elif item.get(key) is None and metric in item:
                    item[key] = item[metric]
            for banned in FORBIDDEN_REPORT_METRICS:
                item.pop(banned, None)
                item.pop(f"{banned}_avg", None)
            item["date"] = day
            rows.append(item)
        return rows

    def _prior_optics(self, job, period):
        override = job.get("payload", {}).get("prior_observations_override")
        if override is not None:
            rows = []
            for row in override:
                if row.get("sensor") != "S2":
                    continue
                if row.get("quality_status") != "eligible_descriptive":
                    continue
                rows.append(row)
            return rows
        periods = self.catalog.periods(job["land_id"], job["season_id"])
        window = bounded_backfill_window(periods, period["period_id"])
        prior_id = window.get("prior_period_id")
        if not prior_id:
            return []
        prior = self.catalog.period(job["land_id"], job["season_id"], prior_id)
        if not prior.get("snapshot"):
            return []
        try:
            snap = self.catalog.load_snapshot(prior["snapshot"])
        except ReportError:
            return []
        rows = [
            row
            for row in snap.get("observations") or []
            if row.get("sensor") == "S2"
            and row.get("quality_status") == "eligible_descriptive"
            and prior["start"] <= str(row.get("date"))[:10] < prior["end_exclusive"]
        ]
        extra = self.catalog.v8_optical_metrics() if period["start"] == "2026-04-04" else None
        dates = (extra or {}).get("dates") or {}
        out = []
        for row in rows:
            item = dict(row)
            day = item["date"][:10]
            for metric in ("ndvi", "evi", "ndre", "ndmi"):
                key = f"{metric}_avg"
                if dates.get(day, {}).get(key) is not None:
                    item[key] = dates[day][key]
            item["date"] = day
            out.append(item)
        return out

    def _write_brief(self, job_id: str, facts: dict, period: dict) -> dict:
        name = f"biweekly-{period['period_id']}-evidence-limited.json"
        rel = Path("artifacts") / f"{job_id}-{name}"
        path = self.work_dir / rel
        body = json.dumps(facts, ensure_ascii=False, indent=2).encode("utf-8")
        path.write_bytes(body)
        return {
            "path": str(rel),
            "filename": name,
            "mime": "application/json",
            "sha256": digest(body),
            "size_bytes": len(body),
            "kind": "evidence_limited_brief",
        }

    def _write_ready_artifact(self, job_id, facts, period, used_reference: bool) -> dict:
        v8_pdf = self.catalog.v8_pdf()
        use_v8 = (
            period["start"] == "2026-04-04"
            and period["end_inclusive"] == "2026-04-17"
            and used_reference
            and v8_pdf is not None
        )
        sidecar = self.work_dir / "artifacts" / f"{job_id}-facts.json"
        sidecar.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
        if use_v8:
            dest = self.work_dir / "artifacts" / f"{job_id}-{v8_pdf.name}"
            shutil.copy2(v8_pdf, dest)
            data = dest.read_bytes()
            return {
                "path": str(dest.relative_to(self.work_dir)),
                "filename": v8_pdf.name,
                "mime": "application/pdf",
                "sha256": digest(data),
                "size_bytes": len(data),
                "kind": "v8_visual_artifact",
                "facts_path": str(sidecar.relative_to(self.work_dir)),
                "note": "视觉稿为已交付的 v8 PDF；阶段与指标取自本次冻结的 facts。",
            }
        name = f"biweekly-{period['period_id']}-facts.json"
        dest = self.work_dir / "artifacts" / f"{job_id}-{name}"
        shutil.copy2(sidecar, dest)
        data = dest.read_bytes()
        return {
            "path": str(dest.relative_to(self.work_dir)),
            "filename": name,
            "mime": "application/json",
            "sha256": digest(data),
            "size_bytes": len(data),
            "kind": "facts_snapshot",
            "note": "本地生成冻结了阶段指标与对照事实；完整 v8 六页版式目前仅在第12期且显式传入参照实例时附带既有 PDF。",
        }
