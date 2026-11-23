"""Official v8 biweekly PDF: wait for bounded backfill, render, upload OSS, register."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from typing import Any

from app.core.logging import logger
from app.worker import celery_app

TERMINAL_SUCCESS = {"completed", "succeeded"}
TERMINAL_FAILURE = {"failed", "cancelled"}
TERMINAL = TERMINAL_SUCCESS | TERMINAL_FAILURE
REGISTRATION_MAX_ATTEMPTS = 8
READY_OFFICIAL = {"ready", "evidence_limited"}



def _is_retry(exc: BaseException) -> bool:
    if type(exc).__name__ == "Retry":
        return True
    try:
        from celery.exceptions import Retry as CeleryRetry
    except ImportError:
        return False
    return isinstance(exc, CeleryRetry)


def _job_status(job: dict | None) -> str:
    if not job:
        return "running"
    return str(job.get("status") or "running")


def _wait_for_backfill(job_ids: list[str]) -> dict[str, str] | None:
    """Return statuses, or None if the status query itself failed."""
    if not job_ids:
        return {}
    try:
        from agric_satellite_analysis_common.internal_api import get_job

        outcomes: dict[str, str] = {}
        for raw in job_ids:
            job_id = str(raw)
            try:
                row = get_job(job_id)
            except Exception:
                return None
            outcomes[job_id] = _job_status(row)
        return outcomes
    except Exception as exc:  # noqa: BLE001
        logger.warning("biweekly_backfill_status_query_failed err_type=%s", type(exc).__name__)
        return None


def _progress_of(job: dict | None) -> dict[str, Any]:
    if not job:
        return {}
    raw = job.get("progress_json") or job.get("progress") or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _load_from_job_progress(report_id: str | None) -> dict[str, Any] | None:
    if not report_id:
        return None
    try:
        from agric_satellite_analysis_common.internal_api import get_job

        job = get_job(report_id)
    except Exception:
        return None
    progress = _progress_of(job)
    from agric_satellite_analysis_common.biweekly.official_ref import recoverable_oss_artifact

    if recoverable_oss_artifact(progress) is None:
        return None
    return progress


def _load_registration(
    report_id: str | None,
    registration_progress: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Prefer the work-item payload passed by the claim agent, then Job progress.

    Job.progress_json is not sufficient: a failed job write must not cause a
    later claim to re-render or re-upload when the work item already has the
    OSS object reference.
    """
    from agric_satellite_analysis_common.biweekly.official_ref import recoverable_oss_artifact

    if recoverable_oss_artifact(registration_progress) is not None:
        return dict(registration_progress)
    return _load_from_job_progress(report_id)


def _registration_backoff_s(attempt: int) -> int:
    from agric_satellite_analysis_common.biweekly.official_ref import registration_backoff_s

    return registration_backoff_s(attempt)


def _business_job_already_complete(report_id: str | None) -> bool:
    if not report_id:
        return False
    try:
        from agric_satellite_analysis_common.internal_api import get_job

        job = get_job(report_id)
    except Exception:
        return False
    return _job_status(job) in (TERMINAL_SUCCESS | {"completed"})


def _align_business_job(
    report_id: str | None,
    *,
    official_status: str,
    oss: dict[str, Any],
    filename: str | None,
) -> bool:
    """Mark the business Job completed after official ready.

    Returns True only when the Job is known completed (this write or a prior
    successful callback). A False return must leave a claimable work item.
    """
    if not report_id:
        return True
    try:
        from app.core.job_http import update_job_record

        update_job_record(
            None,
            None,
            "completed",
            progress={
                "stage": "done",
                "status": official_status,
                "filename": filename,
                "oss_artifact": oss,
            },
            job_id=report_id,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "biweekly_job_complete_persist_failed err_type=%s",
            type(exc).__name__,
        )
    if _business_job_already_complete(report_id):
        return True
    return False


def _complete(
    biweekly_job_id: str,
    *,
    status: str,
    content: bytes | None = None,
    filename: str | None = None,
    mime: str | None = None,
    kind: str | None = None,
    facts: dict | None = None,
    error_code: str | None = None,
    note: str | None = None,
    report_job_id: str | None = None,
    oss: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from agric_satellite_analysis_common.internal_api import complete_biweekly_report

    body: dict[str, Any] = {
        "status": status,
        "error_code": error_code,
        "note": note,
        "filename": filename,
        "mime": mime,
        "kind": kind,
        "facts": facts,
        "report_job_id": report_job_id,
    }
    if oss:
        # New path: notify the API of a permanent object reference; never
        # attach the PDF bytes (or a signed URL) to the complete JSON.
        body["oss"] = {
            "bucket": oss["bucket"],
            "key": oss["key"],
            "size_bytes": int(oss["size_bytes"]),
            "sha256": oss["sha256"],
            "content_type": oss.get("content_type") or mime,
        }
    elif content is not None:
        body["content_base64"] = base64.b64encode(content).decode("ascii")
    return complete_biweekly_report(biweekly_job_id, body)


def _upload_official_object(
    path: Path,
    *,
    land_id: str,
    job_id: str,
    kind: str,
    mime: str,
    data: bytes,
) -> dict[str, Any]:
    from agric_satellite_analysis_common.biweekly.official_ref import official_object_key, sha256_bytes
    from agric_satellite_analysis_common.storage import get_storage
    from app.tasks.storage_tasks import upload_file_via_storage

    key = official_object_key(land_id, job_id, kind=kind)
    upload_file_via_storage(key, str(path), content_type=mime)
    storage = get_storage()
    return {
        "bucket": storage.bucket(),
        "key": key,
        "size_bytes": len(data),
        "sha256": sha256_bytes(data),
        "content_type": mime,
        "kind": kind,
    }


@celery_app.task(
    name="app.tasks.biweekly_report.generate_biweekly_report",
    bind=True,
    max_retries=40,
    default_retry_delay=60,
)
def generate_biweekly_report(
    self,
    job_id: str | None = None,
    mq_task_id: str | None = None,
    work_item_id: str | None = None,
    land_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    biweekly_job_id: str | None = None,
    wait_for_job_ids: list | None = None,
    reference_planting_instance_id: str | None = None,
    report_job_id: str | None = None,
    registration_progress: dict | None = None,
    **_ignored,
) -> dict:
    """Render the official v8 biweekly PDF on the download machine and return it to the API."""
    from app.core.job_http import (
        complete_work_item_http,
        fail_work_item_http,
        progress_work_item_http,
        update_job_record,
    )

    report_id = str(report_job_id or job_id or "") or None
    bj_id = str(biweekly_job_id or "") or None
    land = str(land_id) if land_id else None
    wait_ids = [str(item) for item in (wait_for_job_ids or []) if item]

    def _fail(code: str, note: str) -> dict:
        # Real generate/backfill failure only. Never used after a successful OSS upload.
        if bj_id:
            try:
                _complete(
                    bj_id,
                    status="failed",
                    error_code=code,
                    note=note,
                    report_job_id=report_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("biweekly_complete_failed_call_failed err_type=%s", type(exc).__name__)
        if report_id:
            try:
                update_job_record(None, None, "failed", error=code, job_id=report_id)
            except Exception:
                pass
        fail_work_item_http(work_item_id, note or code)
        return {"status": "failed", "error_code": code}

    def _persist_registration(progress: dict[str, Any], *, attempts: int, error_type: str | None = None) -> None:
        from datetime import datetime, timedelta, timezone

        payload = {
            **progress,
            "stage": "registration_pending",
            "registration_attempts": attempts,
        }
        if error_type:
            payload["last_registration_error"] = error_type
        if attempts >= REGISTRATION_MAX_ATTEMPTS:
            payload["registration_retry_after"] = (
                datetime.now(timezone.utc) + timedelta(seconds=_registration_backoff_s(attempts))
            ).isoformat()
        if report_id:
            try:
                update_job_record(None, None, "running", progress=payload, job_id=report_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "biweekly_job_progress_persist_failed err_type=%s",
                    type(exc).__name__,
                )
        try:
            progress_work_item_http(work_item_id, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "biweekly_work_item_progress_persist_failed err_type=%s",
                type(exc).__name__,
            )

    def _register(progress: dict[str, Any]) -> dict:
        oss = progress["oss_artifact"]
        attempts = int(progress.get("registration_attempts") or 0)
        try:
            result = _complete(
                bj_id,
                status=str(progress.get("official_status") or "ready"),
                filename=progress.get("filename"),
                mime=progress.get("mime"),
                kind=progress.get("kind"),
                facts=progress.get("facts"),
                note=progress.get("note"),
                report_job_id=report_id,
                oss=oss,
            )
        except Exception as exc:  # noqa: BLE001
            if _is_retry(exc):
                raise
            attempts += 1
            try:
                _persist_registration(progress, attempts=attempts, error_type=type(exc).__name__)
            except Exception:
                pass
            if attempts < REGISTRATION_MAX_ATTEMPTS:
                raise self.retry(countdown=_registration_backoff_s(attempts))
            logger.warning(
                "biweekly_registration_exhausted land_id=%s biweekly_job_id=%s attempts=%s err_type=%s",
                land,
                bj_id,
                attempts,
                type(exc).__name__,
            )
            try:
                fail_work_item_http(
                    work_item_id,
                    "REGISTRATION_PENDING",
                    retry=True,
                )
            except Exception:
                pass
            return {
                "status": "registration_pending",
                "error_code": "REGISTRATION_PENDING",
                "attempts": attempts,
            }
        official = (result or {}).get("job") or {}
        official_status = official.get("status") or progress.get("official_status")
        aligned = _align_business_job(
            report_id,
            official_status=str(official_status or "ready"),
            oss=oss,
            filename=progress.get("filename"),
        )
        if not aligned:
            try:
                _persist_registration(
                    {**progress, "official_registered": True, "official_status": official_status},
                    attempts=max(attempts, 1),
                    error_type="JOB_ALIGN_PENDING",
                )
            except Exception:
                pass
            try:
                fail_work_item_http(
                    work_item_id,
                    "JOB_ALIGN_PENDING",
                    retry=True,
                )
            except Exception:
                pass
            # Official is ready, but the business Job is still running.
            # Keep the work item claimable so a later worker can align it.
            return {
                "status": official_status,
                "job": official,
                "job_align": "pending",
            }
        completed = False
        try:
            completed = bool(
                complete_work_item_http(
                    work_item_id,
                    {
                        "status": official_status,
                        "biweekly_job_id": bj_id,
                        "filename": progress.get("filename"),
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "biweekly_work_item_complete_failed err_type=%s",
                type(exc).__name__,
            )
            completed = False
        if not completed:
            try:
                fail_work_item_http(
                    work_item_id,
                    "JOB_ALIGN_PENDING",
                    retry=True,
                )
            except Exception:
                pass
            return {
                "status": official_status,
                "job": official,
                "job_align": "pending",
                "work_item_align": "pending",
            }
        return {"status": official_status, "job": official, "job_align": "done"}

    try:
        prior = _load_registration(report_id, registration_progress)
        if prior and bj_id:
            # Upload already succeeded; retry official registration only.
            return _register(prior)

        if report_id:
            try:
                update_job_record(
                    None,
                    None,
                    "running",
                    progress={"stage": "waiting_for_backfill" if wait_ids else "loading"},
                    job_id=report_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "biweekly_job_running_persist_failed err_type=%s",
                    type(exc).__name__,
                )
        if wait_ids:
            outcomes = _wait_for_backfill(wait_ids)
            if outcomes is None or any(status not in TERMINAL for status in outcomes.values()):
                retries = int(getattr(self.request, "retries", 0) or 0)
                max_r = int(getattr(self, "max_retries", 40) or 40)
                if retries < max_r:
                    raise self.retry(countdown=int(getattr(self, "default_retry_delay", 60) or 60))
                return _fail(
                    "BACKFILL_FAILED",
                    "等待补数任务超时，尚未出报。失败发生在补数阶段，不是因为没有可用光学。",
                )
            successes = [item for item, status in outcomes.items() if status in TERMINAL_SUCCESS]
            if not successes:
                return _fail(
                    "BACKFILL_FAILED",
                    "补数任务全部失败或取消，尚未出报。失败发生在补数阶段，不是因为没有可用光学。",
                )

        if not bj_id:
            return _fail("BODY_INVALID", "缺少 biweekly_job_id，无法回传正式报告。")

        from agric_satellite_analysis_common.internal_api import get_biweekly_report_inputs

        inputs = get_biweekly_report_inputs(bj_id)
        if inputs.get("error_code"):
            code = str(inputs.get("error_code") or "SCENE_QUERY_FAILED")
            note = str(inputs.get("note") or "读取已入库景失败，不能当作本期没有可用光学。")
            return _fail(code, note)

        facts = dict(inputs.get("facts") or {})
        status = str(inputs.get("status") or "")
        land_for_key = str(land or facts.get("land_id") or "")
        if status == "evidence_limited":
            content = (__import__("json").dumps(facts, ensure_ascii=False, indent=2)).encode("utf-8")
            tmp = Path(tempfile.mkdtemp(prefix="biweekly-official-"))
            dest = tmp / "evidence-limited.json"
            try:
                dest.write_bytes(content)
                oss_meta = _upload_official_object(
                    dest,
                    land_id=land_for_key,
                    job_id=bj_id,
                    kind="evidence_limited_brief",
                    mime="application/json",
                    data=content,
                )
            finally:
                try:
                    import shutil

                    shutil.rmtree(tmp, ignore_errors=True)
                except OSError:
                    pass
            progress = {
                "stage": "registration_pending",
                "official_status": "evidence_limited",
                "filename": "evidence-limited.json",
                "mime": "application/json",
                "kind": "evidence_limited_brief",
                "facts": facts,
                "note": "补数阶段已结束。本期确实没有可用光学观测，因此只给出证据受限简报，不是正式 PDF。",
                "oss_artifact": oss_meta,
                "registration_attempts": 0,
            }
            _persist_registration(progress, attempts=0)
            try:
                _complete(
                    bj_id,
                    status="registration_pending",
                    filename=progress.get("filename"),
                    mime=progress.get("mime"),
                    kind=progress.get("kind"),
                    facts=progress.get("facts"),
                    note=progress.get("note"),
                    report_job_id=report_id,
                    oss=progress.get("oss_artifact"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "biweekly_registration_pending_notify_failed err_type=%s",
                    type(exc).__name__,
                )
            return _register(progress)

        if status != "ready" or not facts:
            return _fail("GENERATE_FAILED", "出报输入不完整，补数阶段已经结束，这不是因为没有可用光学。")

        from app.reports.biweekly.generate import generate_official_biweekly_pdf

        tmp = Path(tempfile.mkdtemp(prefix="biweekly-official-"))
        dest = tmp / "official.pdf"
        try:
            generate_official_biweekly_pdf(facts, dest, figures_dir=tmp / "figures")
            content = dest.read_bytes()
            if not content.startswith(b"%PDF") or len(content) < 1000:
                return _fail("GENERATE_FAILED", "正式 PDF 生成失败，补数阶段已经结束。")
            oss_meta = _upload_official_object(
                dest,
                land_id=land_for_key,
                job_id=bj_id,
                kind="official_visual_artifact",
                mime="application/pdf",
                data=content,
            )
        finally:
            try:
                import shutil

                shutil.rmtree(tmp, ignore_errors=True)
            except OSError:
                pass
        start = (facts.get("period") or {}).get("start")
        end = (facts.get("period") or {}).get("end_inclusive")
        filename = f"种植双周报_{facts.get('land_id')}_{start}_{end}.pdf"
        progress = {
            "stage": "registration_pending",
            "official_status": "ready",
            "filename": filename,
            "mime": "application/pdf",
            "kind": "official_visual_artifact",
            "facts": facts,
            "note": "补数阶段已结束，正式报告 PDF 已由下载机生成。",
            "oss_artifact": oss_meta,
            "registration_attempts": 0,
        }
        _persist_registration(progress, attempts=0)
        try:
            _complete(
                bj_id,
                status="registration_pending",
                filename=progress.get("filename"),
                mime=progress.get("mime"),
                kind=progress.get("kind"),
                facts=progress.get("facts"),
                note=progress.get("note"),
                report_job_id=report_id,
                oss=progress.get("oss_artifact"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "biweekly_registration_pending_notify_failed err_type=%s",
                type(exc).__name__,
            )
        return _register(progress)
    except Exception as exc:
        if _is_retry(exc):
            raise
        from agric_satellite_analysis_common.biweekly.models import ReportError

        prior = _load_registration(report_id, registration_progress)
        if prior:
            # OSS already holds the PDF; a later error is a registration problem.
            logger.warning(
                "biweekly_registration_error_after_upload land_id=%s biweekly_job_id=%s err_type=%s",
                land,
                bj_id,
                type(exc).__name__,
            )
            return _register(prior)
        if isinstance(exc, ReportError):
            code = exc.code or "GENERATE_FAILED"
            note = "出报失败，补数阶段已经结束。" if code != "SCENE_QUERY_FAILED" else "读取已入库景失败，不能当作本期没有可用光学。"
            return _fail(code, note)
        logger.exception(
            "biweekly_report_failed land_id=%s biweekly_job_id=%s report_job_id=%s",
            land,
            bj_id,
            report_id,
        )
        return _fail("GENERATE_FAILED", "出报失败，补数阶段已经结束。")
