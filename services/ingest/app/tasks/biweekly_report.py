"""Official v8 biweekly PDF: wait for bounded backfill, render, return to API."""

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
    if content is not None:
        body["content_base64"] = base64.b64encode(content).decode("ascii")
    return complete_biweekly_report(biweekly_job_id, body)


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
    **_ignored,
) -> dict:
    """Render the official v8 biweekly PDF on the download machine and return it to the API."""
    from app.core.job_http import complete_work_item_http, fail_work_item_http, update_job_record

    report_id = str(report_job_id or job_id or "") or None
    bj_id = str(biweekly_job_id or "") or None
    land = str(land_id) if land_id else None
    wait_ids = [str(item) for item in (wait_for_job_ids or []) if item]

    def _fail(code: str, note: str) -> dict:
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

    try:
        if report_id:
            update_job_record(
                None,
                None,
                "running",
                progress={"stage": "waiting_for_backfill" if wait_ids else "loading"},
                job_id=report_id,
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
        if status == "evidence_limited":
            content = (__import__("json").dumps(facts, ensure_ascii=False, indent=2)).encode("utf-8")
            result = _complete(
                bj_id,
                status="evidence_limited",
                content=content,
                filename="evidence-limited.json",
                mime="application/json",
                kind="evidence_limited_brief",
                facts=facts,
                note="补数阶段已结束。本期确实没有可用光学观测，因此只给出证据受限简报，不是正式 PDF。",
                report_job_id=report_id,
            )
            complete_work_item_http(work_item_id, {"status": "evidence_limited", "biweekly_job_id": bj_id})
            if report_id:
                update_job_record(
                    None,
                    None,
                    "completed",
                    progress={"stage": "done", "status": "evidence_limited"},
                    job_id=report_id,
                )
            return {"status": "evidence_limited", "job": (result or {}).get("job")}

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
            # Reuse the download machine's existing storage config when available;
            # the source of truth for the user download is still the API artifact.
            try:
                from app.tasks.storage_tasks import upload_file_via_storage

                object_key = f"reports/default/{land or facts.get('land_id')}/biweekly-{bj_id}.pdf"
                upload_file_via_storage(object_key, str(dest), content_type="application/pdf")
            except Exception as exc:  # noqa: BLE001
                logger.warning("biweekly_oss_upload_skipped err_type=%s", type(exc).__name__)
        finally:
            try:
                import shutil

                shutil.rmtree(tmp, ignore_errors=True)
            except OSError:
                pass
        start = (facts.get("period") or {}).get("start")
        end = (facts.get("period") or {}).get("end_inclusive")
        filename = f"种植双周报_{facts.get('land_id')}_{start}_{end}.pdf"

        result = _complete(
            bj_id,
            status="ready",
            content=content,
            filename=filename,
            mime="application/pdf",
            kind="official_visual_artifact",
            facts=facts,
            note="补数阶段已结束，正式报告 PDF 已由下载机生成。",
            report_job_id=report_id,
        )
        complete_work_item_http(
            work_item_id,
            {"status": "ready", "biweekly_job_id": bj_id, "filename": filename},
        )
        if report_id:
            update_job_record(
                None,
                None,
                "completed",
                progress={"stage": "done", "status": "ready", "filename": filename},
                job_id=report_id,
            )
        return {"status": "ready", "job": (result or {}).get("job")}
    except Exception as exc:
        if type(exc).__name__ == "Retry":
            raise
        from agric_satellite_analysis_common.biweekly.models import ReportError

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
