"""Celery 任务：触发 API 侧总览预聚合，下载机不直连 Postgres。"""

from __future__ import annotations

from typing import Any

import structlog

from app.worker import celery_app

logger = structlog.get_logger()


@celery_app.task(
    name="app.tasks.overview_preagg.refresh_daily_satellite",
    bind=True,
    max_retries=300,
    time_limit=600,
    soft_time_limit=540,
)
def refresh_daily_satellite(
    self, run_id: str | None = None, as_of: str | None = None
) -> dict[str, Any]:
    """每天拉取全量地块的新影像，延时检查入库，完成后由API计算快照。"""
    from agric_satellite_analysis_common.internal_api import (
        daily_satellite_finalize,
        daily_satellite_prepare,
        internal_api_enabled,
    )

    if not internal_api_enabled():
        raise RuntimeError("每日全国刷新需要API_BASE_URL和INTERNAL_API_TOKEN")
    # 首次触发固定北京时间统计日；跨日重试继续同一批次，不能创建下一天的下载。
    if not as_of:
        from datetime import datetime, timedelta, timezone

        as_of = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    try:
        if run_id is None:
            prepared = daily_satellite_prepare(as_of=as_of)
            run_id = prepared["run_id"]
        out = daily_satellite_finalize(run_id)
    except Exception as exc:
        logger.exception("overview_daily_http_failed", run_id=run_id, as_of=as_of)
        raise self.retry(
            exc=exc, kwargs={"run_id": run_id, "as_of": as_of}, countdown=300
        )
    if out["status"] not in ("completed", "partial"):
        # 用消息延时而非占用worker等待；MQ结果真正入库之后才发布当天态势。
        raise self.retry(kwargs={"run_id": run_id, "as_of": as_of}, countdown=300)
    logger.info(
        "overview_daily_finished",
        run_id=run_id,
        status=out["status"],
        regions=out.get("regions"),
    )
    return out


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
        from agric_satellite_analysis_common.internal_api import (
            internal_api_enabled,
            refresh_overview_stats as refresh_overview_http,
        )
    except ImportError as exc:
        raise RuntimeError(
            "refresh_overview_stats 需要 agric_satellite_analysis_common.internal_api"
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
