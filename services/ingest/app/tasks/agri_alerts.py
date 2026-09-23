"""下载 Worker 只通过 Internal HTTP 请求 API 重算预警。"""

from __future__ import annotations

from typing import Any

import structlog

from app.worker import celery_app

logger = structlog.get_logger()


def evaluate_agri_rs_alerts_for_land(
    land_id: str,
    *,
    replace_open: bool = True,
) -> dict[str, Any]:
    """把重算请求交给 API 主库执行，下载机不读取或写入 PostgreSQL。"""
    from agric_satellite_analysis_common.internal_api import (
        evaluate_land_alerts,
        internal_api_enabled,
    )

    if not internal_api_enabled():
        raise RuntimeError("agri alert evaluation requires API_BASE_URL and INTERNAL_API_TOKEN")
    return evaluate_land_alerts(land_id, replace_open=replace_open)


@celery_app.task(
    name="app.tasks.agri_alerts.evaluate_agri_alerts_for_land",
    bind=True,
    max_retries=2,
    time_limit=300,
    soft_time_limit=240,
)
def evaluate_agri_alerts_for_land(
    self,
    land_id: str,
    scene_date: str | None = None,
    replace_open: bool = True,
) -> dict[str, Any]:
    """Celery 接口兼容旧任务消息；实际重算统一在 API 上完成。"""
    try:
        result = evaluate_agri_rs_alerts_for_land(
            land_id,
            replace_open=replace_open,
        )
        logger.info(
            "agri_alerts_evaluated_via_api",
            land_id=land_id,
            scene_date=scene_date,
            created=result.get("created"),
            removed_open=result.get("removed_open"),
        )
        return result
    except Exception as exc:
        logger.exception("agri_alerts_api_evaluation_failed", land_id=land_id)
        raise self.retry(exc=exc, countdown=60)
