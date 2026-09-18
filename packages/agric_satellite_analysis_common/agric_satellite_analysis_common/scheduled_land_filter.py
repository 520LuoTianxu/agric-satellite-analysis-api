"""定时任务统一地块过滤规则。

这些规则只约束自动同步、自动下载和自动计算，不删除数据库中的历史地块数据，
也不改变管理员手工查询地块的权限。
"""

from __future__ import annotations

import math
from typing import Any

# 这些基地不参与任何自动化遥感、天气、指数和报告任务。
EXCLUDED_SCHEDULE_BASE_IDS = frozenset(
    {"4", "6", "10", "17", "28", "33", "52", "62"}
)
MAX_SCHEDULE_LAND_AREA_MU = 5_000.0


def is_excluded_schedule_base_id(base_id: Any) -> bool:
    """Return whether a base ID is explicitly excluded from scheduled work."""
    if base_id is None:
        return False
    return str(base_id).strip() in EXCLUDED_SCHEDULE_BASE_IDS


def is_scheduled_land_allowed(base_id: Any, land_area_mu: Any) -> bool:
    """判断地块是否允许进入自动化任务队列。"""
    if is_excluded_schedule_base_id(base_id):
        return False
    if land_area_mu is None:
        return True
    try:
        area = float(land_area_mu)
    except (TypeError, ValueError):
        # 面积无法解析时交给原有数据校验处理，不因为过滤器误伤地块。
        return True
    return not (math.isfinite(area) and area > MAX_SCHEDULE_LAND_AREA_MU)


def scheduled_land_sql(alias: str = "p", *, area_param: str = "max_schedule_area_mu") -> str:
    """Return a PostgreSQL predicate for scheduled-land queries."""
    base_ids = ", ".join(f"'{value}'" for value in sorted(EXCLUDED_SCHEDULE_BASE_IDS))
    return (
        f"(({alias}.base_id IS NULL) OR trim({alias}.base_id::text) NOT IN ({base_ids})) "
        f"AND coalesce({alias}.land_area_mu, 0) <= :{area_param}"
    )


__all__ = [
    "EXCLUDED_SCHEDULE_BASE_IDS",
    "MAX_SCHEDULE_LAND_AREA_MU",
    "is_excluded_schedule_base_id",
    "is_scheduled_land_allowed",
    "scheduled_land_sql",
]
