"""Celery Beat 入口：请求 API 机按虚拟项目区调度五年历史回填。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from agric_satellite_analysis_common.internal_api import (
    internal_api_enabled,
    virtual_area_history_backfill,
)
from app.core.admin_task_tracking import track_admin_task_run
from app.worker import celery_app

logger = structlog.get_logger()


def _execution_key(_args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """同一北京时间日期只创建一次 Beat 运行记录。"""
    if kwargs.get("as_of"):
        return str(kwargs["as_of"])
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


@celery_app.task(
    name="app.tasks.virtual_area.schedule_virtual_area_history_backfill",
    bind=True,
    max_retries=3,
    time_limit=300,
    soft_time_limit=240,
)
@track_admin_task_run(
    task_key="virtual-area-history",
    task_name="app.tasks.virtual_area.schedule_virtual_area_history_backfill",
    execution_key=_execution_key,
    params=lambda _args, kwargs: {"as_of": kwargs.get("as_of")},
)
def schedule_virtual_area_history_backfill(
    self,
    *,
    as_of: str | None = None,
    admin_task_run_id: str | None = None,
) -> dict[str, Any]:
    """只在 API 机执行规划/发任务，下载机不接触 PostgreSQL 或 Smart 凭据。"""
    if not internal_api_enabled():
        raise RuntimeError("虚拟项目区历史回填需要 API_BASE_URL 和 INTERNAL_API_TOKEN")
    try:
        result = virtual_area_history_backfill(as_of=as_of)
    except Exception as exc:
        logger.exception("virtual_area_history_http_failed", as_of=as_of)
        retry_kwargs: dict[str, Any] = {"as_of": as_of}
        if admin_task_run_id:
            retry_kwargs["admin_task_run_id"] = admin_task_run_id
        raise self.retry(exc=exc, kwargs=retry_kwargs, countdown=300)
    logger.info(
        "virtual_area_history_dispatched",
        parent_job_id=result.get("parent_job_id"),
        area_count=result.get("area_count"),
        job_count=result.get("job_count"),
    )
    return result
