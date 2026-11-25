"""Official v8 biweekly PDF: wait for bounded backfill, render, upload OSS, register."""

from __future__ import annotations

import base64
import hashlib
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


def _artifact_error_code(exc: BaseException) -> str | None:
    text = str(exc)
    for code in (
        "ARTIFACT_SIZE_MISMATCH",
        "ARTIFACT_CONFLICT",
        "ARTIFACT_CORRUPT",
        "ARTIFACT_UNAVAILABLE",
    ):
        if code in text:
            return code
    return None


def _refresh_oss_artifact(oss: dict[str, Any]) -> dict[str, Any]:
    """Replace size/sha256 with the live OSS object after an overwrite.

    Registration must not keep a stale checksum from an earlier upload of the
    same key. If the object cannot be read, the original ref is kept.
    """
    if not oss or not oss.get("key"):
        return oss
    from agric_satellite_analysis_common.storage import get_storage

    storage = get_storage()
    key = str(oss["key"])
    if not storage.exists(key):
        return oss
    stat = storage.stat_object(key)
    hasher = hashlib.sha256()
    seen = 0
    for chunk in storage.iter_bytes(key):
        hasher.update(chunk)
        seen += len(chunk)
    size = int(stat.get("size_bytes") or 0) or seen
    if seen and size != seen:
        size = seen
    updated = dict(oss)
    updated["size_bytes"] = size
    updated["sha256"] = hasher.hexdigest()
    if stat.get("content_type") and not updated.get("content_type"):
        updated["content_type"] = stat["content_type"]
    return updated


def _try_refresh_oss_artifact(oss: dict[str, Any]) -> dict[str, Any]:
    try:
        return _refresh_oss_artifact(oss)
    except Exception:  # noqa: BLE001
        _log_exc("biweekly_oss_refresh_failed")
        return oss


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
        logger.warning(
            "biweekly_backfill_status_query_failed err_type=%s", type(exc).__name__
        )
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
    from agric_satellite_analysis_common.biweekly.official_ref import (
        recoverable_oss_artifact,
    )

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
    from agric_satellite_analysis_common.biweekly.official_ref import (
        recoverable_oss_artifact,
    )

    if recoverable_oss_artifact(registration_progress) is not None:
        return dict(registration_progress)
    return _load_from_job_progress(report_id)


def _registration_backoff_s(attempt: int) -> int:
    from agric_satellite_analysis_common.biweekly.official_ref import (
        registration_backoff_s,
    )

    return registration_backoff_s(attempt)


def _retry_kwargs(task: Any, progress: dict[str, Any]) -> dict[str, Any]:
    """Celery retry kwargs that keep the original claim plus the OSS ref.

    ``self.retry(kwargs=...)`` replaces the message kwargs. Dropping land_id /
    biweekly_job_id here would make the next delivery unable to register.
    """
    request = getattr(task, "request", None)
    raw = getattr(request, "kwargs", None) if request is not None else None
    base = dict(raw) if isinstance(raw, dict) else {}
    base["registration_progress"] = progress
    return base


def _bucket_from_upload_uri(uploaded: Any, key: str) -> str | None:
    """Bucket encoded in ``upload_file_via_storage``'s ``uri`` (``oss://bucket/key``).

    ``OssStorage.uri_for`` writes that string from the same bucket name the
    property exposes, and only after ``upload_file`` has returned.
    """
    if not isinstance(uploaded, dict):
        return None
    uri = str(uploaded.get("uri") or "")
    prefix = "oss://"
    if not uri.startswith(prefix):
        return None
    bucket, sep, object_key = uri[len(prefix) :].partition("/")
    if not sep or not bucket or "/" in bucket or object_key != key:
        return None
    return bucket


def _soil_is_ready(facts: dict[str, Any]) -> bool:
    soil = facts.get("soil_context")
    return isinstance(soil, dict) and soil.get("status") == "ready"


def _fetch_soil_for_land(land_id: str) -> dict[str, Any]:
    from app.tasks.soil import fetch_soil_for_land

    return fetch_soil_for_land.run(land_id)


def _fill_missing_soil(
    bj_id: str, facts: dict[str, Any], land_id: str
) -> dict[str, Any]:
    """Pull the field soil archive once when the report would otherwise omit it.

    A failed fetch still returns the original facts. The conclusion must not
    invent soil numbers, and this does not open the API database directly.
    """
    if _soil_is_ready(facts) or not bj_id or not land_id:
        return facts
    try:
        from agric_satellite_analysis_common.internal_api import (
            get_biweekly_report_inputs,
            internal_api_enabled,
        )
    except Exception:  # noqa: BLE001
        return facts
    if not internal_api_enabled():
        return facts
    try:
        result = _fetch_soil_for_land(land_id)
    except Exception as exc:  # noqa: BLE001 — 土壤补拉失败不挡住已经能出的报告
        logger.warning(
            "biweekly_soil_fetch_failed",
            land_id=land_id,
            error_type=type(exc).__name__,
        )
        return facts
    if not isinstance(result, dict) or result.get("status") != "success":
        logger.warning(
            "biweekly_soil_fetch_empty",
            land_id=land_id,
            status=result.get("status") if isinstance(result, dict) else None,
        )
        return facts
    try:
        again = get_biweekly_report_inputs(bj_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "biweekly_soil_reread_failed",
            land_id=land_id,
            error_type=type(exc).__name__,
        )
        return facts
    if again.get("error_code"):
        return facts
    refreshed = dict(again.get("facts") or {})
    return refreshed or facts


def _log_exc(event: str, **fields: Any) -> None:
    """Log the active exception with a redacted type, message, and stack."""
    logger.exception(event, **fields)


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
    except Exception:  # noqa: BLE001
        _log_exc("biweekly_job_complete_persist_failed")
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
    from agric_satellite_analysis_common.biweekly.official_ref import (
        official_object_key,
        sha256_bytes,
    )
    from agric_satellite_analysis_common.storage import bucket_name, get_storage
    from app.tasks.storage_tasks import upload_file_via_storage

    key = official_object_key(land_id, job_id, kind=kind)
    # 哈希在上传前算好：上传函数一旦返回，对象已经在 OSS，不能再依赖临时文件。
    digest = sha256_bytes(data)
    uploaded = upload_file_via_storage(key, str(path), content_type=mime)
    # 真实返回是 {key, public_url, backend, uri}，uri 形如 oss://{bucket}/{key}。
    # bucket 是 str 属性，不能调用。属性读失败时用这条 uri，上传本身已经成功。
    uri_bucket = _bucket_from_upload_uri(uploaded, key)
    try:
        bucket = bucket_name(get_storage())
    except Exception:
        _log_exc(
            "biweekly_oss_bucket_property_failed",
            key=key,
            land_id=land_id,
            biweekly_job_id=job_id,
        )
        if not uri_bucket:
            raise
        bucket = uri_bucket
    else:
        if uri_bucket and uri_bucket != bucket:
            logger.warning(
                "biweekly_oss_bucket_uri_mismatch",
                key=key,
                property_bucket=bucket,
                uri_bucket=uri_bucket,
            )
    return {
        "bucket": bucket,
        "key": key,
        "size_bytes": len(data),
        "sha256": digest,
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
            except Exception:  # noqa: BLE001
                _log_exc("biweekly_complete_failed_call_failed")
        if report_id:
            try:
                update_job_record(None, None, "failed", error=code, job_id=report_id)
            except Exception:
                pass
        fail_work_item_http(work_item_id, note or code)
        return {"status": "failed", "error_code": code}

    def _persist_registration(
        progress: dict[str, Any], *, attempts: int, error_type: str | None = None
    ) -> dict[str, Any]:
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
                datetime.now(timezone.utc)
                + timedelta(seconds=_registration_backoff_s(attempts))
            ).isoformat()
        if report_id:
            try:
                update_job_record(
                    None, None, "running", progress=payload, job_id=report_id
                )
            except Exception:  # noqa: BLE001
                _log_exc("biweekly_job_progress_persist_failed")
        try:
            progress_work_item_http(work_item_id, payload)
        except Exception:  # noqa: BLE001
            _log_exc("biweekly_work_item_progress_persist_failed")
        return payload

    def _retry_registration(progress: dict[str, Any], attempts: int) -> None:
        raise self.retry(
            countdown=_registration_backoff_s(max(attempts, 1)),
            kwargs=_retry_kwargs(self, progress),
        )

    def _register(progress: dict[str, Any]) -> dict:
        oss = _try_refresh_oss_artifact(dict(progress["oss_artifact"]))
        progress = {**progress, "oss_artifact": oss}
        attempts = int(progress.get("registration_attempts") or 0)
        result = None
        exc = None
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
        except Exception as first:  # noqa: BLE001
            if _is_retry(first):
                raise
            # 覆盖后若仍带着旧 size 去登记，API 会按现对象头校验并返回 409。
            # 再读一次活对象，用新 size/hash 重试一次，不重新渲染。
            if _artifact_error_code(first) == "ARTIFACT_SIZE_MISMATCH":
                try:
                    oss = _try_refresh_oss_artifact(oss)
                    progress = {**progress, "oss_artifact": oss}
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
                except Exception as second:  # noqa: BLE001
                    if _is_retry(second):
                        raise
                    exc = second
            else:
                exc = first
        if exc is not None:
            attempts += 1
            saved = _persist_registration(
                progress, attempts=attempts, error_type=type(exc).__name__
            )
            if attempts < REGISTRATION_MAX_ATTEMPTS:
                _retry_registration(saved, attempts)
            try:
                raise exc
            except Exception:
                _log_exc(
                    "biweekly_registration_exhausted",
                    land_id=land,
                    biweekly_job_id=bj_id,
                    attempts=attempts,
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
                    {
                        **progress,
                        "official_registered": True,
                        "official_status": official_status,
                    },
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
        except Exception:  # noqa: BLE001
            _log_exc("biweekly_work_item_complete_failed")
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

    committed: dict[str, Any] | None = None
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
                    progress={
                        "stage": "waiting_for_backfill" if wait_ids else "loading"
                    },
                    job_id=report_id,
                )
            except Exception:  # noqa: BLE001
                _log_exc("biweekly_job_running_persist_failed")
        if wait_ids:
            outcomes = _wait_for_backfill(wait_ids)
            if outcomes is None or any(
                status not in TERMINAL for status in outcomes.values()
            ):
                retries = int(getattr(self.request, "retries", 0) or 0)
                max_r = int(getattr(self, "max_retries", 40) or 40)
                if retries < max_r:
                    raise self.retry(
                        countdown=int(getattr(self, "default_retry_delay", 60) or 60)
                    )
                return _fail(
                    "BACKFILL_FAILED",
                    "等待补数任务超时，尚未出报。失败发生在补数阶段，不是因为没有可用光学。",
                )
            successes = [
                item for item, status in outcomes.items() if status in TERMINAL_SUCCESS
            ]
            if not successes:
                return _fail(
                    "BACKFILL_FAILED",
                    "补数任务全部失败或取消，尚未出报。失败发生在补数阶段，不是因为没有可用光学。",
                )

        if not bj_id:
            return _fail("BODY_INVALID", "缺少 biweekly_job_id，无法回传正式报告。")

        from agric_satellite_analysis_common.internal_api import (
            get_biweekly_report_inputs,
        )

        inputs = get_biweekly_report_inputs(bj_id)
        if inputs.get("error_code"):
            code = str(inputs.get("error_code") or "SCENE_QUERY_FAILED")
            note = str(
                inputs.get("note") or "读取已入库景失败，不能当作本期没有可用光学。"
            )
            return _fail(code, note)

        facts = dict(inputs.get("facts") or {})
        status = str(inputs.get("status") or "")
        land_for_key = str(land or facts.get("land_id") or "")
        if status == "ready":
            # 没有土壤档案时先拉一次再出结论，不要只用“缺失”把别的数据补上去。
            facts = _fill_missing_soil(bj_id, facts, land_for_key)
        if status == "evidence_limited":
            content = (
                __import__("json").dumps(facts, ensure_ascii=False, indent=2)
            ).encode("utf-8")
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
            # 先记住内存中的 OSS 引用，再写 work item。写失败也不能回到生成失败。
            committed = progress
            committed = _persist_registration(progress, attempts=0)
            try:
                _complete(
                    bj_id,
                    status="registration_pending",
                    filename=committed.get("filename"),
                    mime=committed.get("mime"),
                    kind=committed.get("kind"),
                    facts=committed.get("facts"),
                    note=committed.get("note"),
                    report_job_id=report_id,
                    oss=committed.get("oss_artifact"),
                )
            except Exception:  # noqa: BLE001
                _log_exc("biweekly_registration_pending_notify_failed")
            return _register(committed)

        if status != "ready" or not facts:
            return _fail(
                "GENERATE_FAILED",
                "出报输入不完整，补数阶段已经结束，这不是因为没有可用光学。",
            )

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
        note = "补数阶段已结束，正式报告 PDF 已由下载机生成。"
        extra = str((facts.get("ai_narrative") or {}).get("reader_note") or "").strip()
        if extra:
            note = f"{note} {extra}"
        progress = {
            "stage": "registration_pending",
            "official_status": "ready",
            "filename": filename,
            "mime": "application/pdf",
            "kind": "official_visual_artifact",
            "facts": facts,
            "note": note,
            "oss_artifact": oss_meta,
            "registration_attempts": 0,
        }
        # 先记住内存中的 OSS 引用，再写 work item。写失败也不能回到生成失败。
        committed = progress
        committed = _persist_registration(progress, attempts=0)
        try:
            _complete(
                bj_id,
                status="registration_pending",
                filename=committed.get("filename"),
                mime=committed.get("mime"),
                kind=committed.get("kind"),
                facts=committed.get("facts"),
                note=committed.get("note"),
                report_job_id=report_id,
                oss=committed.get("oss_artifact"),
            )
        except Exception:  # noqa: BLE001
            _log_exc("biweekly_registration_pending_notify_failed")
        return _register(committed)
    except Exception as exc:
        if _is_retry(exc):
            raise
        from agric_satellite_analysis_common.biweekly.models import ReportError
        from agric_satellite_analysis_common.biweekly.official_ref import (
            recoverable_oss_artifact,
        )

        prior = committed if recoverable_oss_artifact(committed) else None
        if prior is None:
            prior = _load_registration(report_id, registration_progress)
        if prior and recoverable_oss_artifact(prior):
            # 上传已经成功。这里的异常是登记/对齐问题，不能写成 GENERATE_FAILED，
            # 也不能把 OSS 引用丢掉。先写入 work item，再只重试登记。
            _log_exc(
                "biweekly_registration_error_after_upload",
                land_id=land,
                biweekly_job_id=bj_id,
                report_job_id=report_id,
            )
            saved = _persist_registration(
                prior,
                attempts=int(prior.get("registration_attempts") or 0),
                error_type=type(exc).__name__,
            )
            return _register(saved)
        if isinstance(exc, ReportError):
            code = exc.code or "GENERATE_FAILED"
            note = (
                "出报失败，补数阶段已经结束。"
                if code != "SCENE_QUERY_FAILED"
                else "读取已入库景失败，不能当作本期没有可用光学。"
            )
            return _fail(code, note)
        _log_exc(
            "biweekly_report_failed",
            land_id=land,
            biweekly_job_id=bj_id,
            report_job_id=report_id,
        )
        return _fail("GENERATE_FAILED", "出报失败，补数阶段已经结束。")
