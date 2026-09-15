"""Celery 任务：触发 API 侧总览预聚合，下载机不直连 Postgres。"""

from __future__ import annotations

from typing import Any

import structlog

from app.worker import celery_app

logger = structlog.get_logger()


@celery_app.task(name="app.tasks.overview_preagg.refresh_overview_stats")
def refresh_overview_stats(
    window_days: int = 60,
    crop: str | None = None,
) -> dict[str, Any]:
    """请 API 预聚合全国和分省总览。

    计算和 overview_stats_daily 写入都在 API。Beat 只触发本任务，
    下载机不得查库。
    """
    try:
        from openfarm_common.internal_api import (
            internal_api_enabled,
            refresh_overview_stats as refresh_overview_http,
        )
    except ImportError as exc:
        raise RuntimeError(
            "refresh_overview_stats 需要 openfarm_common.internal_api"
        ) from exc

    if not internal_api_enabled():
        raise RuntimeError(
            "refresh_overview_stats 需要 API_BASE_URL 和 INTERNAL_API_TOKEN；"
            "总览预聚合在 API 上跑，不在下载机查 Postgres"
        )

    logger.info(
        "overview_preagg_start",
        window_days=window_days,
        crop=crop,
        http=True,
    )
    out = refresh_overview_http(window_days=window_days, crop=crop)
    logger.info(
        "overview_preagg_done",
        regions=out.get("regions"),
        http=True,
    )
    return out
