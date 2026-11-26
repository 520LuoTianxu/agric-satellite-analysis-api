"""定时任务统一地块过滤规则。

这些规则只约束自动同步、自动下载和自动计算，不删除数据库中的历史地块数据，
也不改变管理员手工查询地块的权限或租户 base_id 隔离。
面积超过阈值的地块不进入自动化队列；不再按基地编号硬编码排除。
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

# 面积超过该阈值的地块不进入自动化遥感、天气、指数和报告任务。
# 不再按基地编号硬编码排除地块。
MAX_SCHEDULE_LAND_AREA_MU = 5_000.0

BYPASS_SCHEDULE_LAND_FILTER_PARAM = "bypass_schedule_land_filter"


def should_bypass_schedule_land_filter(params: Any) -> bool:
    """Return whether a job payload asked to skip the scheduled-land exclusion.

    User-triggered jobs may set this flag (biweekly backfill, manual autumn
    emergence). Automatic daily or scheduled sync, weather, and report jobs
    must not pass it.
    """
    if not isinstance(params, dict):
        return False
    return bool(
        params.get(BYPASS_SCHEDULE_LAND_FILTER_PARAM)
        or params.get("allow_excluded_bases")
    )


def is_scheduled_land_allowed(base_id: Any, land_area_mu: Any) -> bool:
    """判断地块是否允许进入自动化任务队列。

    ``base_id`` 保留以兼容既有调用方，不再作为排除条件。
    """
    if land_area_mu is None:
        return True
    try:
        area = float(land_area_mu)
    except (TypeError, ValueError):
        # 面积无法解析时交给原有数据校验处理，不因为过滤器误伤地块。
        return True
    return not (math.isfinite(area) and area > MAX_SCHEDULE_LAND_AREA_MU)


def scheduled_land_sql(alias: str = "p", *, area_param: str = "max_schedule_area_mu") -> str:
    """Return a PostgreSQL predicate for the scheduled-land area limit."""
    return f"coalesce({alias}.land_area_mu, 0) <= :{area_param}"


def scheduled_date_window(
    latest: date | None,
    *,
    today: date,
    stale_days: int = 7,
) -> tuple[date, date] | None:
    """Return the incremental remote-sensing window for a scheduled land.

    Weather scheduling reuses this exact window so a scheduled weather pull
    cannot silently fall back to yesterday/today while the RS task backfills a
    longer gap.
    """
    threshold = today - timedelta(days=stale_days)
    if latest is not None and latest > threshold:
        return None
    date_from = (latest + timedelta(days=1)) if latest else threshold
    if date_from >= today:
        return None
    return date_from, today


__all__ = [
    "MAX_SCHEDULE_LAND_AREA_MU",
    "BYPASS_SCHEDULE_LAND_FILTER_PARAM",
    "is_scheduled_land_allowed",
    "should_bypass_schedule_land_filter",
    "scheduled_date_window",
    "scheduled_land_sql",
]
