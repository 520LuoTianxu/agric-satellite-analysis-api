"""Explicitly triggered biweekly jobs. Nothing is auto-published.

Local fixture mode only: plans bounded backfill and builds snapshots from the
6417 pack. It never dispatches production satellite_batch, never writes a
business database, and never scans lands on a timer.
"""

from __future__ import annotations

import json
import secrets
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .calendar import add_days, bounded_backfill_window, periods_closed_before, today_shanghai
from .catalog import ALLOWED_REFERENCE_IDS, LocalPackCatalog
from .models import ReportError, canonical, digest
from .official_ref import (
    READY_STATUSES,
    OfficialStoreNotMigrated,
    OfficialStoreUnavailable,
    inspect_official_object,
    official_object_key,
    public_artifact,
    sha256_bytes,
)
from .official_store import latest_brief
from .phase_policy import (
    FORBIDDEN_REPORT_METRICS,
    POLICY_VERSION,
    compare_to_prior,
    display_phase,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
STATES = (
    "queued",
    "running",
    "dispatched",
    "registration_pending",
    "ready",
    "failed",
    "evidence_limited",
)


def _repo_root() -> Path:
    # .../packages/agric_satellite_analysis_common/agric_satellite_analysis_common/biweekly/trigger.py
    return Path(__file__).resolve().parents[4]


def reference_fixture_path() -> Path:
    return _repo_root() / "examples" / "biweekly" / "reference_plan_15933.json"


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _fact_observation(row: dict) -> dict:
    """Compact per-date optical row for report facts. Missing values stay None."""
    return {
        "date": str(row.get("date"))[:10],
        "sensor": row.get("sensor"),
        "ndvi_avg": row.get("ndvi_avg"),
        "evi_avg": row.get("evi_avg"),
        "ndre_avg": row.get("ndre_avg"),
        "ndmi_avg": row.get("ndmi_avg"),
        "cloud_cover": row.get("cloud_cover"),
        "parcel_cloud_cover_pct": row.get("parcel_cloud_cover_pct"),
    }


class TriggerService:
    """In-process, file-backed job store for the local trigger API."""

    def __init__(
        self,
        work_dir: Path,
        catalog: LocalPackCatalog | None = None,
        *,
        auto_run=True,
        pdf_renderer=None,
        official_store=None,
        object_storage=None,
        expected_oss_bucket: str | None = None,
    ):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        (self.work_dir / "jobs").mkdir(exist_ok=True)
        (self.work_dir / "artifacts").mkdir(exist_ok=True)
        self.catalog = catalog or LocalPackCatalog()
        self.auto_run = auto_run
        self.pdf_renderer = pdf_renderer
        self.official_store = official_store
        self.object_storage = object_storage
        if expected_oss_bucket:
            self.expected_oss_bucket = expected_oss_bucket
        elif object_storage is not None:
            from agric_satellite_analysis_common.storage import bucket_name

            self.expected_oss_bucket = bucket_name(object_storage)
        else:
            self.expected_oss_bucket = None

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
        stored = self._store_get(job_id)
        if stored is not None:
            return self._sanitize_job(stored)
        path = self.work_dir / "jobs" / f"{job_id}.json"
        if not path.is_file():
            raise ReportError("JOB_NOT_FOUND")
        job = json.loads(path.read_text())
        # Pre-migration file jobs are not claimed as moved into the official table.
        job.setdefault("persistence", "legacy_work_dir")
        job.setdefault("migrated", False)
        return self._sanitize_job(job)

    def _store_get(self, job_id: str) -> dict | None:
        if self.official_store is None:
            return None
        try:
            return self.official_store.get(job_id)
        except OfficialStoreNotMigrated:
            return None
        except OfficialStoreUnavailable as exc:
            raise ReportError("OFFICIAL_STORE_UNAVAILABLE") from exc

    def _sanitize_job(self, job: dict) -> dict:
        out = dict(job)
        if out.get("artifact"):
            out["artifact"] = public_artifact(out["artifact"])
        return out

    def download(self, job_id: str) -> tuple[str, bytes, str]:
        filename, chunks, mime, _size = self.iter_download(job_id)
        return filename, b"".join(chunks), mime

    def iter_download(self, job_id: str):
        """Yield artifact bytes for a same-origin authenticated download.

        OSS objects are streamed from the configured bucket; a signed URL is
        never returned, so the frontend same-origin Bearer download stays valid.
        """
        job = self.get_job(job_id)
        if job["status"] not in READY_STATUSES:
            raise ReportError("ARTIFACT_UNAVAILABLE")
        artifact = job.get("artifact") or {}
        filename = artifact.get("filename") or f"{job_id}.bin"
        mime = (
            artifact.get("mime")
            or artifact.get("content_type")
            or "application/octet-stream"
        )
        if artifact.get("key"):
            return (
                filename,
                self._iter_oss_artifact(job, artifact),
                mime,
                artifact.get("size_bytes"),
            )
        rel = artifact.get("path")
        if not rel:
            raise ReportError("ARTIFACT_UNAVAILABLE")
        path = self.work_dir / rel
        data = path.read_bytes()
        if artifact.get("sha256") and digest(data) != artifact.get("sha256"):
            raise ReportError("ARTIFACT_CORRUPT")
        return filename, iter((data,)), mime, len(data)

    def _iter_oss_artifact(self, job: dict, artifact: dict):
        if self.object_storage is None:
            raise ReportError("ARTIFACT_UNAVAILABLE")
        kind = artifact.get("kind") or "official_visual_artifact"
        expected_key = official_object_key(job["land_id"], job["job_id"], kind=kind)
        if artifact.get("key") != expected_key:
            raise ReportError("ARTIFACT_KEY_MISMATCH")
        if (
            self.expected_oss_bucket
            and artifact.get("bucket") != self.expected_oss_bucket
        ):
            raise ReportError("ARTIFACT_BUCKET_MISMATCH")
        for chunk in self.object_storage.iter_bytes(artifact["key"]):
            yield chunk

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
        should_evaluate = (
            self.auto_run if evaluate_local_fixture is None else evaluate_local_fixture
        )
        if should_evaluate:
            job = self.run_job(job["job_id"])
        return job

    def request_season_backfill(
        self, land_id: str, season_id: str, as_of: str | None = None
    ) -> dict:
        """Queue a season-scoped backfill job without evaluating local fixtures.

        Only periods that have already ended before as_of are listed. An open
        period would ask the download host for imagery through a future day.
        """
        periods = self.catalog.periods(land_id, season_id)
        if not periods:
            raise ReportError("SEASON_NOT_FOUND")
        as_of = str(as_of or today_shanghai())[:10]
        closed = periods_closed_before(periods, as_of)
        windows = []
        for row in closed:
            window = bounded_backfill_window(periods, row["period_id"])
            # 结束日当天仍可能没有完整影像，窗口停在 as_of 之前。
            if window["date_to_inclusive"] >= as_of:
                continue
            windows.append({"period_id": row["period_id"], "window": window})
        closed_ids = [item["period_id"] for item in windows]
        if windows:
            by_id = {row["period_id"]: row for row in closed}
            first = by_id[windows[0]["period_id"]]
            last = by_id[windows[-1]["period_id"]]
            span = {
                "period_id": None,
                "start": first["start"],
                "end_inclusive": last["end_inclusive"],
                "end_exclusive": last["end_exclusive"],
            }
        else:
            span = {
                "period_id": None,
                "start": as_of,
                "end_inclusive": as_of,
                "end_exclusive": add_days(as_of, 1),
            }
        job = self._create(
            kind="backfill_season",
            land_id=land_id,
            season_id=season_id,
            period=span,
            payload={
                "scope": "season",
                "as_of": as_of,
                "windows": windows,
                "period_ids": closed_ids,
                "skipped_period_ids": [
                    row["period_id"] for row in periods if row["period_id"] not in set(closed_ids)
                ],
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
        hard_fail = reason in {
            "DISPATCH_FAILED",
            "LAND_NOT_AVAILABLE",
            "BACKFILL_WINDOW_UNBOUNDED",
        } or (not dispatched and reason not in {None, "NO_JOBS_CREATED"})
        result = dict(job.get("result") or {})
        result["stage"] = "backfill"
        result["backfill"] = {
            "status": "failed"
            if hard_fail
            else ("dispatched" if dispatched else "not_needed"),
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
            result["note"] = (
                "补数失败，尚未出报。失败发生在补数阶段，不是因为没有可用光学，也不是出报本身失败。"
            )
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

    def fail_stage(
        self, job_id: str, error_code: str, *, note: str | None = None
    ) -> dict:
        job = self.get_job(job_id)
        if job.get("status") in READY_STATUSES:
            return job
        if job.get("status") == "registration_pending" and (
            job.get("artifact") or {}
        ).get("sha256"):
            return job
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

    def merge_result(
        self, job_id: str, patch: dict, *, status: str | None = None
    ) -> dict:
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
        if self.official_store is not None:
            try:
                job["persistence"] = "database"
                job.pop("migrated", None)
                stored = self.official_store.upsert(job)
                job.clear()
                job.update(stored)
            except OfficialStoreNotMigrated:
                job["persistence"] = "legacy_work_dir"
                job["migrated"] = False
            except OfficialStoreUnavailable as exc:
                raise ReportError("OFFICIAL_STORE_UNAVAILABLE") from exc
        else:
            job.setdefault("persistence", "legacy_work_dir")
            job.setdefault("migrated", False)
        path = self.work_dir / "jobs" / f"{job['job_id']}.json"
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")

    def _transition(self, job, status, result=None, artifact=None, error_code=None):
        if status not in STATES:
            raise ReportError("STATE_INVALID")
        if status == "failed" and job.get("status") in READY_STATUSES:
            return job
        if (
            status == "failed"
            and job.get("status") == "registration_pending"
            and (job.get("artifact") or {}).get("sha256")
        ):
            return job
        if self.official_store is not None:
            event = dict(job)
            event["status"] = status
            if result is not None:
                event["result"] = result
            if artifact is not None:
                event["artifact"] = artifact
            if error_code is not None:
                event["error_code"] = error_code
            try:
                stored, outcome = self.official_store.apply_event(job["job_id"], event)
            except OfficialStoreNotMigrated:
                stored, outcome = None, "legacy"
            except OfficialStoreUnavailable as exc:
                raise ReportError("OFFICIAL_STORE_UNAVAILABLE") from exc
            else:
                if outcome == "ignored_stale_fail":
                    job.clear()
                    job.update(stored)
                    self._write_file_cache(job)
                    return job
                job.clear()
                job.update(stored)
                self._write(job)
                return job
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
        return job

    def _write_file_cache(self, job: dict) -> None:
        path = self.work_dir / "jobs" / f"{job['job_id']}.json"
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")

    def _latest_for_period(self, land_id, season_id, period_id):
        file_latest = None
        for path in sorted((self.work_dir / "jobs").glob("bj-*.json")):
            job = json.loads(path.read_text())
            if (
                job.get("land_id") == str(land_id)
                and job.get("season_id") == season_id
                and job.get("period_id") == str(period_id)
                and job.get("kind") == "generate"
            ):
                brief = latest_brief(job)
                brief["persistence"] = job.get("persistence") or "legacy_work_dir"
                file_latest = brief
        store_latest = None
        if self.official_store is not None:
            try:
                stored = self.official_store.latest_for_period(
                    land_id, season_id, period_id
                )
            except OfficialStoreNotMigrated:
                stored = None
            except OfficialStoreUnavailable as exc:
                raise ReportError("OFFICIAL_STORE_UNAVAILABLE") from exc
            else:
                store_latest = latest_brief(stored)
        if (
            store_latest
            and file_latest
            and store_latest["job_id"] != file_latest["job_id"]
        ):
            # A leftover file job is not silently treated as migrated; pick the newer one.
            store_ts = store_latest.get("created_at") or ""
            file_ts = file_latest.get("created_at") or ""
            return store_latest if store_ts >= file_ts else file_latest
        return store_latest or file_latest

    def period_for_job(self, job: dict) -> dict:
        """Use dates persisted on the generate job; catalog.period is a fallback only.

        Official finalize and GET /internal/biweekly/jobs/{id}/inputs build a
        TriggerService without SmartPlantingCatalog. Smart lands such as 32696
        are not in the local 6417 pack, so catalog.period would raise
        PERIOD_NOT_FOUND even though the job already stored its official window.
        Never invent a calendar when the window is missing.
        """
        window = job.get("window") or {}
        start = window.get("start")
        end_inclusive = window.get("end_inclusive")
        end_exclusive = window.get("end_exclusive")
        if start and (end_inclusive or end_exclusive):
            if not end_exclusive and end_inclusive:
                end_exclusive = (
                    date.fromisoformat(str(end_inclusive)) + timedelta(days=1)
                ).isoformat()
            if not end_inclusive and end_exclusive:
                end_inclusive = (
                    date.fromisoformat(str(end_exclusive)) - timedelta(days=1)
                ).isoformat()
            if str(start) >= str(end_exclusive):
                raise ReportError("PERIOD_WINDOW_INVALID")
            return {
                "period_id": str(job.get("period_id")),
                "start": str(start),
                "end_inclusive": str(end_inclusive),
                "end_exclusive": str(end_exclusive),
                "snapshot": None,
            }
        return self.catalog.period(job["land_id"], job["season_id"], job["period_id"])

    def periods_from_stored_jobs(self, land_id: str, season_id: str) -> list[dict]:
        rows: list[dict] = []
        seen: set[str] = set()
        jobs_dir = self.work_dir / "jobs"
        if not jobs_dir.is_dir():
            return rows
        for path in sorted(jobs_dir.glob("bj-*.json")):
            try:
                job = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if (
                str(job.get("land_id")) != str(land_id)
                or job.get("season_id") != season_id
                or job.get("kind") != "generate"
            ):
                continue
            try:
                period = self.period_for_job(job)
            except ReportError:
                continue
            key = str(period.get("period_id"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(period)
        rows.sort(
            key=lambda row: (
                str(row.get("start") or ""),
                str(row.get("period_id") or ""),
            )
        )
        return rows

    def periods_for_job(self, job: dict) -> list[dict]:
        try:
            return list(self.catalog.periods(job["land_id"], job["season_id"]))
        except ReportError:
            rows = self.periods_from_stored_jobs(job["land_id"], job["season_id"])
            if rows:
                return rows
            return [self.period_for_job(job)]

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
            if str(row.get("sensor") or "").strip().upper() == "S2"
            and row.get("quality_status") == "eligible_descriptive"
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
        facts, status = self._prepare_generate_facts(job)
        period = self.period_for_job(job)
        if status == "evidence_limited":
            artifact = self._write_brief(job["job_id"], facts, period)
        else:
            artifact = self._write_ready_artifact(
                job["job_id"], facts, period, bool(facts.get("used_reference"))
            )
        facts["content_sha256"] = digest(canonical(facts))
        facts["generate"] = {
            "status": status,
            "error_code": None,
            "artifact_kind": artifact.get("kind"),
        }
        return facts, artifact, status

    def _prepare_generate_facts(self, job: dict) -> tuple[dict, str]:
        period = self.period_for_job(job)
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
            cutoff = snapshot.get("observation_cutoff") or (
                period["end_exclusive"] + "T00:00:00+08:00"
            )
        if not cutoff.endswith("+08:00") and "+08" not in cutoff:
            raise ReportError("CUTOFF_TIMEZONE_INVALID")
        observations = []
        rejected_optical = []
        radar_observations = []
        for row in source_rows:
            day = str(row.get("date"))[:10]
            if day >= period["end_exclusive"] or day < period["start"]:
                continue
            if str(row.get("sensor") or "").strip().upper() == "S1":
                # 同期查询已经带回的雷达景。这里只转交，不再查库。
                metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
                radar_observations.append(
                    {
                        "date": day,
                        "scene_id": row.get("scene_id"),
                        "stac_item_id": row.get("stac_item_id"),
                        "relative_orbit": row.get("relative_orbit"),
                        "platform": row.get("platform"),
                        "vv": row.get("vv_avg") if row.get("vv_avg") is not None else metrics.get("vv"),
                        "vh": row.get("vh_avg") if row.get("vh_avg") is not None else metrics.get("vh"),
                        "processing_version": row.get("processing_version"),
                        "calibration_epoch": row.get("calibration_epoch"),
                        "calibration_method": row.get("calibration_method"),
                        "calibration_scale": row.get("calibration_scale"),
                    }
                )
                continue
            if str(row.get("sensor") or "").strip().upper() != "S2":
                continue
            if row.get("quality_status") != "eligible_descriptive":
                # 未采用的光学景也要留痕，报告才能如实说明本期有几次过境、几次因云量等被剔除。
                rejected_optical.append(
                    {
                        "date": day,
                        "reason": row.get("exclusion_reason") or "excluded",
                        "cloud_cover": row.get("cloud_cover"),
                        "parcel_cloud_cover_pct": row.get("parcel_cloud_cover_pct"),
                    }
                )
                continue
            observations.append(row)

        enriched = self._attach_v8_metrics(period, observations)
        reference = None
        ref_id = job["payload"].get("reference_planting_instance_id")
        if ref_id:
            reference = self._load_reference(ref_id)

        prior_rows = self._prior_optics(job, period) if enriched else []
        prior_dates = [str(row.get("date"))[:10] for row in prior_rows]

        native = job.get("payload", {}).get("native_plan") or None
        if native and native.get("intervals"):
            phase = display_phase(
                planned_intervals=native["intervals"],
                observation_dates=[row["date"][:10] for row in enriched],
                window_start=period["start"],
                end_exclusive=period["end_exclusive"],
                prior_observation_dates=prior_dates,
            )
            phase["phenology_basis"] = (
                native.get("phenology_basis") or "PLAN_DERIVED_FROM_SMART_PLANT_TASK"
            )
            phase["plan_source"] = "native"
            phase["plan_label"] = "本田方案"
        elif reference:
            phase = display_phase(
                planned_intervals=reference["intervals"],
                observation_dates=[row["date"][:10] for row in enriched],
                window_start=period["start"],
                end_exclusive=period["end_exclusive"],
                prior_observation_dates=prior_dates,
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
        metrics_for_compare = [
            m for m in metrics_for_compare if m not in FORBIDDEN_REPORT_METRICS
        ]
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
            "crop_name": payload.get("crop_name")
            or (payload.get("native_plan") or {}).get("crop_name"),
            "crop_type": payload.get("crop_type"),
            "season_id": job["season_id"],
            "period": period,
            "policy_version": POLICY_VERSION,
            "cutoff": cutoff,
            "optical_days": [row["date"][:10] for row in enriched],
            "observations": [_fact_observation(row) for row in enriched],
            # 对比基准的逐日观测与本期一并下发，图表和文字才能用同一组日期，不各取各的。
            "prior_observations": [_fact_observation(row) for row in prior_rows],
            "rejected_optical": rejected_optical,
            "phase": phase,
            "prior_comparison": prior_cmp,
            "weather_summary": snapshot.get("weather_summary")
            or payload.get("weather_summary"),
            "weather_daily": payload.get("weather_daily"),
            "weather_period": payload.get("weather_period"),
            "weather_antecedent": payload.get("weather_antecedent"),
            "s1_count": s1_count,
            "in_period_radar": int(s1_count or 0) > 0,
            # 只转交调用方已经放进载荷的雷达景和同田往年归档，不在这里查询或回填。
            "radar_observations": radar_observations,
            "historical_same_period": payload.get("historical_same_period")
            if isinstance(payload.get("historical_same_period"), list)
            else None,
            "sowing_date": payload.get("sowing_date"),
            "science_limits": snapshot.get("science_limits"),
            "forbidden_metrics_omitted": sorted(FORBIDDEN_REPORT_METRICS),
            "generated_at": _now(),
            "observation_source": payload.get("observation_source")
            or ("pack_snapshot" if snapshot else "none"),
        }
        if enriched and len(enriched) < 2 and prior_cmp:
            facts["quality_note"] = "仅1次有效观测，不能写成持续改善。"

        if not enriched:
            facts["brief"] = (
                "本期没有可用光学观测。此为证据受限简报，不作出长势好转或阶段诊断。"
            )
            if (period.get("s1_products") or 0) == 0:
                facts["radar_note"] = (
                    "本期没有雷达影像，不对内涝风险作出偏低或偏高的结论。"
                )
            status = "evidence_limited"
        else:
            status = "ready"
            facts["brief"] = None
        facts["stage"] = "done"
        facts["generate"] = {
            "status": status,
            "error_code": None,
            "artifact_kind": (
                "official_visual_artifact"
                if status == "ready"
                else "evidence_limited_brief"
            ),
        }
        facts["used_reference"] = bool(reference)
        return facts, status

    def prepare_generate_facts(self, job: dict) -> tuple[dict, str]:
        """Build official-report facts without writing an artifact or rendering a PDF."""
        return self._prepare_generate_facts(job)

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
        try:
            periods = self.catalog.periods(job["land_id"], job["season_id"])
        except ReportError:
            return []
        ids = [row["period_id"] for row in periods]
        try:
            index = ids.index(str(period["period_id"]))
        except ValueError:
            return []
        extra = (
            self.catalog.v8_optical_metrics()
            if period["start"] == "2026-04-04"
            else None
        )
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

    def attach_oss_artifact(
        self,
        job_id: str,
        *,
        status: str,
        artifact: dict,
        facts: dict | None = None,
        error_code: str | None = None,
        note: str | None = None,
        inspect: bool = True,
    ) -> dict:
        """Register a pre-uploaded official OSS object and close the official job."""
        job = self.get_job(job_id)
        if job.get("kind") != "generate":
            raise ReportError("JOB_KIND_UNSUPPORTED")
        kind = artifact.get("kind") or (
            "official_visual_artifact"
            if status == "ready"
            else "evidence_limited_brief"
        )
        ref = {
            "storage": "oss",
            "bucket": artifact["bucket"],
            "key": artifact["key"],
            "size_bytes": artifact["size_bytes"],
            "sha256": artifact["sha256"],
            "content_type": artifact.get("content_type") or artifact.get("mime"),
            "mime": artifact.get("mime") or artifact.get("content_type"),
            "filename": artifact.get("filename"),
            "kind": kind,
        }
        if inspect:
            if self.object_storage is None:
                raise ReportError("ARTIFACT_UNAVAILABLE")
            inspected = inspect_official_object(
                self.object_storage,
                ref,
                expect_pdf=(kind == "official_visual_artifact"),
            )
            ref.update(inspected)
            ref["mime"] = ref.get("mime") or ref.get("content_type")
        result = dict(job.get("result") or {})
        result.update(facts or {})
        result["stage"] = "done"
        result["generate"] = {
            "status": status,
            "error_code": error_code,
            "artifact_kind": kind,
        }
        if note:
            result["note"] = note
        self._transition(
            job,
            status,
            result=result,
            artifact=public_artifact(ref),
            error_code=error_code,
        )
        return self.get_job(job_id)

    def attach_remote_artifact(
        self,
        job_id: str,
        *,
        status: str,
        content: bytes,
        filename: str,
        mime: str,
        kind: str,
        facts: dict | None = None,
        error_code: str | None = None,
        note: str | None = None,
    ) -> dict:
        """Legacy complete path: accept raw bytes (old download machines still send base64)."""
        job = self.get_job(job_id)
        if job.get("kind") != "generate":
            raise ReportError("JOB_KIND_UNSUPPORTED")
        dest_dir = self.work_dir / "artifacts"
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_name = filename or (
            f"{job_id}.pdf" if kind == "official_visual_artifact" else f"{job_id}.json"
        )
        dest = dest_dir / f"{job_id}-{safe_name}"
        dest.write_bytes(content)
        artifact = {
            "path": str(dest.relative_to(self.work_dir)),
            "filename": safe_name,
            "mime": mime,
            "sha256": sha256_bytes(content),
            "size_bytes": len(content),
            "kind": kind,
            "storage": "work_dir",
        }
        if self.object_storage is not None and self.expected_oss_bucket:
            key = official_object_key(job["land_id"], job_id, kind=kind)
            try:
                if hasattr(self.object_storage, "put_bytes"):
                    self.object_storage.put_bytes(key, content, content_type=mime)
                else:
                    dest_tmp = dest
                    self.object_storage.upload_file(
                        key, str(dest_tmp), content_type=mime
                    )
                artifact.update(
                    {
                        "storage": "oss",
                        "bucket": self.expected_oss_bucket,
                        "key": key,
                        "content_type": mime,
                    }
                )
            except Exception:
                # Keep the legacy local artifact so an old machine can still complete;
                # restart-safe OSS registration will be missing until retried.
                artifact["storage"] = "work_dir"
        if facts:
            sidecar = dest_dir / f"{job_id}-facts.json"
            sidecar.write_text(
                json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            artifact["facts_path"] = str(sidecar.relative_to(self.work_dir))
        result = dict(job.get("result") or {})
        result.update(facts or {})
        result["stage"] = "done"
        result["generate"] = {
            "status": status,
            "error_code": error_code,
            "artifact_kind": kind,
        }
        if note:
            result["note"] = note
        if status == "failed":
            self._transition(
                job, "failed", result=result, artifact=artifact, error_code=error_code
            )
        else:
            self._transition(
                job, status, result=result, artifact=artifact, error_code=error_code
            )
        return self.get_job(job_id)

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

    def _write_ready_artifact(
        self, job_id, facts, period, used_reference: bool
    ) -> dict:
        sidecar = self.work_dir / "artifacts" / f"{job_id}-facts.json"
        sidecar.write_text(
            json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
