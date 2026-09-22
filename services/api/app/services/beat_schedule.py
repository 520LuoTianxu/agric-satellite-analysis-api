"""下载机 Beat 用的计划辅助：查库只发生在 API 侧。"""

from __future__ import annotations

from datetime import date

from agric_satellite_analysis_common.scheduled_land_filter import scheduled_date_window

# 最新栅格超过这么多天视为过期，需要补拉
STALE_DAYS = 7
# 各地块投递间隔，避免同时打满 STAC
STAGGER_SECONDS = 15

# 一个规范地块只对应一个光学任务；任务内部计算全部指数。
WEEKLY_INDEX_KEYS: tuple[str, ...] = ("agri_optical",)


def index_task_name(key: str) -> str:
    """Canonical land parcel index worker task name."""
    if key != "agri_optical":
        raise ValueError(f"unsupported canonical index task: {key}")
    return "app.tasks.agri_lonlat.process_agri_optical_lonlat"


def weekly_date_window(
    latest: date | None,
    *,
    today: date,
    stale_days: int = STALE_DAYS,
) -> tuple[date, date] | None:
    """过期地块返回 (起始日, 结束日)；仍新鲜则返回 None。"""
    return scheduled_date_window(latest, today=today, stale_days=stale_days)
