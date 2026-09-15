"""下载机 Beat 用的计划辅助：查库只发生在 API 侧。"""

from __future__ import annotations

from datetime import date, timedelta

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
    threshold = today - timedelta(days=stale_days)
    if latest is not None and latest > threshold:
        return None
    date_from = (
        (latest + timedelta(days=1))
        if latest
        else (today - timedelta(days=stale_days))
    )
    date_to = today
    if date_from >= date_to:
        return None
    return date_from, date_to
