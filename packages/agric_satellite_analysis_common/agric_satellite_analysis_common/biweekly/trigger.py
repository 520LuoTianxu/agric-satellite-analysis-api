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
STATES = ("queued", "running", "dispatched", "ready", "failed", "evidence_limited")


def _repo_root() -> Path:
    # .../packages/agric_satellite_analysis_common/agric_satellite_analysis_common/biweekly/trigger.py
    return Path(__file__).resolve().parents[4]


def reference_fixture_path() -> Path:
    return _repo_root() / "examples" / "biweekly" / "reference_plan_15933.json"


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


class TriggerService:
    """In-process, file-backed job store for the local trigger API."""

    def __init__(
        self,
        work_dir: Path,
        catalog: LocalPackCatalog | None = None,
        *,
        auto_run=True,
        pdf_renderer=None,
    ):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        (self.work_dir / "jobs").mkdir(exist_ok=True)
        (self.work_dir / "artifacts").mkdir(exist_ok=True)
        self.catalog = catalog or LocalPackCatalog()
        self.auto_run = auto_run
        self.pdf_renderer = pdf_renderer

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

    def request_backfill(
        self,
        land_id: str,
        season_id: str,
        period_id: str,
        *,
        evaluate_local_fixture: bool | None = None,
    ) -> dict:
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
            payload={"planned_batch": planned, "scope": "period"},
        )
        should_evaluate = self.auto_run if evaluate_local_fixture is None else evaluate_local_fixture
        if should_evaluate:
            job = self.run_job(job["job_id"])
        return job

    def request_season_backfill(self, land_id: str, season_id: str) -> dict:
        """Queue a season-scoped backfill job without evaluating local fixtures."""
        periods = self.catalog.periods(land_id, season_id)
        if not periods:
            raise ReportError("SEASON_NOT_FOUND")
        windows = []
        for row in periods:
            window = bounded_backfill_window(periods, row["period_id"])
            windows.append({"period_id": row["period_id"], "window": window})
        first, last = periods[0], periods[-1]
        job = self._create(
            kind="backfill_season",
            land_id=land_id,
            season_id=season_id,
            period={
                "period_id": None,
                "start": first["start"],
                "end_inclusive": last["end_inclusive"],
                "end_exclusive": last["end_exclusive"],
            },
            payload={
                "scope": "season",
                "windows": windows,
                "period_ids": [row["period_id"] for row in periods],
            },
        )
        # Never auto-run the local snapshot evaluation for a season fill.
        return job

    def record_backfill_dispatch(self, job_id: str, dispatch: dict) -> dict:
        """Persist production dispatch outcome. Do not evaluate local snapshots."""
        job = self.get_job(job_id)
        dispatched = bool(dispatch.get("dispatched"))
        payload = dict(job.get("payload") or {})
        planned = dict(payload.get("planned_batch") or {})
        if planned:
            planned["dispatch"] = "satellite_batch" if dispatched else "dispatch_failed"
            payload["planned_batch"] = planned
        job["payload"] = payload
        result = dict(job.get("result") or {})
        result.update(dispatch)
        result["mode"] = "production_dispatch"
        result["dispatched_to_production"] = dispatched
        result["satellite_batch_job_ids"] = list(dispatch.get("job_ids") or [])
        if planned:
            result["planned_batch"] = planned
        status = "dispatched" if dispatched else "failed"
        error = None if dispatched else (dispatch.get("reason") or "DISPATCH_FAILED")
        self._transition(job, status, result=result, error_code=error)
        job["dispatched_to_production"] = dispatched
        job["satellite_batch_job_ids"] = list(dispatch.get("job_ids") or [])
        self._write(job)
        return self.get_job(job_id)

    def record_generate_backfill(self, job_id: str, dispatch: dict) -> dict:
        """Record the backfill stage of a generate job. Do not run generate here."""
        job = self.get_job(job_id)
        reason = dispatch.get("reason")
        job_ids = list(dispatch.get("job_ids") or [])
        dispatched = bool(dispatch.get("dispatched")) and bool(job_ids)
        hard_fail = reason in {"DISPATCH_FAILED", "LAND_NOT_AVAILABLE", "BACKFILL_WINDOW_UNBOUNDED"} or (
            not dispatched and reason not in {None, "NO_JOBS_CREATED"}
        )
        result = dict(job.get("result") or {})
        result["stage"] = "backfill"
        result["backfill"] = {
            "status": "failed" if hard_fail else ("dispatched" if dispatched else "not_needed"),
            "error_code": (reason or "BACKFILL_FAILED") if hard_fail else None,
            "reason": reason,
            "job_ids": job_ids,
            "date_from": dispatch.get("date_from"),
            "date_to": dispatch.get("date_to"),
        }
        result["dispatched_to_production"] = dispatched
        result["satellite_batch_job_ids"] = job_ids
        result["generate"] = {"status": "pending", "error_code": None}
        if hard_fail:
            result["note"] = "补数失败，尚未出报。失败发生在补数阶段，不是因为没有可用光学，也不是出报本身失败。"
            self._transition(job, "failed", result=result, error_code="BACKFILL_FAILED")
        elif dispatched:
            result["note"] = "本期补数已派发，出报将在补数完成后自动进行。"
            self._transition(job, "dispatched", result=result)
        else:
            result["note"] = "本期没有新的补数任务需要派发，进入出报阶段。"
            self._transition(job, "queued", result=result)
        job["satellite_batch_job_ids"] = job_ids
        job["dispatched_to_production"] = dispatched
        self._write(job)
        return self.get_job(job_id)

    def fail_stage(self, job_id: str, error_code: str, *, note: str | None = None) -> dict:
        job = self.get_job(job_id)
        result = dict(job.get("result") or {})
        if error_code == "BACKFILL_FAILED":
            result["stage"] = "backfill"
            backfill = dict(result.get("backfill") or {})
            backfill["status"] = "failed"
            backfill["error_code"] = error_code
            result["backfill"] = backfill
        else:
            result["stage"] = "generate"
            generate = dict(result.get("generate") or {})
            generate["status"] = "failed"
            generate["error_code"] = error_code
            result["generate"] = generate
        if note:
            result["note"] = note
        self._transition(job, "failed", result=result, error_code=error_code)
        return self.get_job(job_id)

    def merge_result(self, job_id: str, patch: dict, *, status: str | None = None) -> dict:
        job = self.get_job(job_id)
        result = dict(job.get("result") or {})
        result.update(patch)
        if status:
            self._transition(job, status, result=result)
        else:
            job["result"] = result
            self._write(job)
        return self.get_job(job_id)

    def set_payload_fields(self, job_id: str, **fields) -> dict:
        job = self.get_job(job_id)
        payload = dict(job.get("payload") or {})
        payload.update(fields)
        job["payload"] = payload
        self._write(job)
        return self.get_job(job_id)

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
        land_name: str | None = None,
        weather_daily: list | None = None,
        weather_period: dict | None = None,
        weather_antecedent: dict | None = None,
        s1_count: int | None = None,
        parent_season_job_id: str | None = None,
        defer_run: bool = False,
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
                "land_name": land_name,
                "weather_daily": weather_daily,
                "weather_period": weather_period,
                "weather_antecedent": weather_antecedent,
                "s1_count": s1_count,
                "parent_season_job_id": parent_season_job_id,
            },
        )
        if self.auto_run and not defer_run:
            job = self.run_job(job["job_id"])
        return job

    def run_job(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if job["status"] not in {"queued", "running", "dispatched"}:
            return job
        if job["status"] == "dispatched" and job.get("kind") != "generate":
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
            if str(row.get("sensor") or "").strip().upper() == "S2" and row.get("quality_status") == "eligible_descriptive"
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
            if str(row.get("sensor") or "").strip().upper() != "S2":
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

        payload = job.get("payload") or {}
        s1_count = payload.get("s1_count")
        if s1_count is None:
            s1_count = int(period.get("s1_products") or 0)
        facts = {
            "schema_version": "biweekly-trigger-snapshot-v1",
            "land_id": job["land_id"],
            "land_name": payload.get("land_name"),
            "season_id": job["season_id"],
            "period": period,
            "policy_version": POLICY_VERSION,
            "cutoff": cutoff,
            "optical_days": [row["date"][:10] for row in enriched],
            "observations": [
                {
                    "date": row["date"][:10],
                    "sensor": row.get("sensor"),
                    "ndvi_avg": row.get("ndvi_avg"),
                    "evi_avg": row.get("evi_avg"),
                    "ndre_avg": row.get("ndre_avg"),
                    "ndmi_avg": row.get("ndmi_avg"),
                }
                for row in enriched
            ],
            "phase": phase,
            "prior_comparison": prior_cmp,
            "weather_summary": snapshot.get("weather_summary") or payload.get("weather_summary"),
            "weather_daily": payload.get("weather_daily"),
            "weather_period": payload.get("weather_period"),
            "weather_antecedent": payload.get("weather_antecedent"),
            "s1_count": s1_count,
            "in_period_radar": int(s1_count or 0) > 0,
            "science_limits": snapshot.get("science_limits"),
            "forbidden_metrics_omitted": sorted(FORBIDDEN_REPORT_METRICS),
            "generated_at": _now(),
            "observation_source": payload.get("observation_source") or ("pack_snapshot" if snapshot else "none"),
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
        facts["stage"] = "done"
        facts["generate"] = {
            "status": status,
            "error_code": None,
            "artifact_kind": artifact.get("kind"),
        }
        if status == "ready":
            facts["brief"] = None
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
                if str(row.get("sensor") or "").strip().upper() != "S2":
                    continue
                if row.get("quality_status") != "eligible_descriptive":
                    continue
                rows.append(row)
            return rows
        periods = self.catalog.periods(job["land_id"], job["season_id"])
        ids = [row["period_id"] for row in periods]
        try:
            index = ids.index(str(period["period_id"]))
        except ValueError:
            return []
        extra = self.catalog.v8_optical_metrics() if period["start"] == "2026-04-04" else None
        dates = (extra or {}).get("dates") or {}
        for prior in reversed(periods[:index]):
            if not prior.get("snapshot"):
                continue
            try:
                snap = self.catalog.load_snapshot(prior["snapshot"])
            except ReportError:
                continue
            rows = [
                row
                for row in snap.get("observations") or []
                if str(row.get("sensor") or "").strip().upper() == "S2"
                and row.get("quality_status") == "eligible_descriptive"
                and prior["start"] <= str(row.get("date"))[:10] < prior["end_exclusive"]
            ]
            if not rows:
                continue
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
        return []

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
        sidecar = self.work_dir / "artifacts" / f"{job_id}-facts.json"
        sidecar.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.pdf_renderer is None:
            raise ReportError("PDF_RENDERER_UNAVAILABLE")
        start = period.get("start")
        end = period.get("end_inclusive") or period.get("end_exclusive")
        name = f"种植双周报_{facts.get('land_id')}_{start}_{end}.pdf"
        dest = self.work_dir / "artifacts" / f"{job_id}-{name}"
        self.pdf_renderer(facts, dest)
        data = dest.read_bytes()
        return {
            "path": str(dest.relative_to(self.work_dir)),
            "filename": name,
            "mime": "application/pdf",
            "sha256": digest(data),
            "size_bytes": len(data),
            "kind": "official_visual_artifact",
            "facts_path": str(sidecar.relative_to(self.work_dir)),
            "note": "正式六页 PDF。阶段指标按 S1/S2/S3 选定；参照方案仅在调用方明确传入时使用并已标注参照。",
            "used_reference": bool(used_reference),
        }
