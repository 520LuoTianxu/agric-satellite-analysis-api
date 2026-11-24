"""Load one land's filled key-node forms from Smart MySQL.

Runs on the API machine. The download worker only receives the compact
result through the internal inputs payload.
"""

from __future__ import annotations

from typing import Any

from agric_satellite_analysis_common.biweekly.field_records import (
    parse_workbox_rows,
    workbox_season_keys,
)
from agric_satellite_analysis_common.biweekly.models import ReportError
from sqlalchemy import bindparam, text

from app.core.config import settings
from app.core.logging import logger
from app.services.biweekly_plans import exact_land_id

_SQL = text(
    """
    SELECT
        n.task_code,
        n.task_name_snapshot,
        n.node_no,
        n.task_status,
        n.stage_code,
        n.available_date,
        n.submitted_at,
        n.land_id,
        n.executor_json,
        n.branch_manager_json,
        n.regional_manager_json
    FROM agriculture_workbox_task_node AS n
    JOIN agriculture_planting_workbox AS w
        ON w.id = n.workbox_id
       AND w.base_id = n.base_id
       AND w.del_flag = '0'
    WHERE n.land_id = :land_id
      AND n.task_scope_type = 'LAND'
      AND n.del_flag = '0'
      AND w.crop_season_key IN :season_keys
      AND (
          TRIM(n.executor_json) NOT IN ('', '{}', 'null')
          OR TRIM(n.branch_manager_json) NOT IN ('', '{}', 'null')
          OR TRIM(n.regional_manager_json) NOT IN ('', '{}', 'null')
      )
    ORDER BY n.sort_no, n.task_code, n.node_no, n.id
    LIMIT 16
    """
).bindparams(bindparam("season_keys", expanding=True))


def empty_key_nodes(
    status: str, season_keys: list[str] | None = None
) -> dict[str, Any]:
    return {
        "status": status,
        "season_keys": list(season_keys or []),
        "nodes": [],
        "photos": [],
    }


async def load_key_node_records(
    land_id: str,
    season_id: str,
    *,
    crop_name: str | None = None,
    fetch_rows: Any | None = None,
) -> dict[str, Any]:
    """Filled land nodes only. Missing Smart config does not fail the report."""
    try:
        keys = workbox_season_keys(season_id, crop_name)
        land_key = exact_land_id(land_id)
    except ReportError:
        return empty_key_nodes("land_invalid")
    if not keys:
        return empty_key_nodes("season_unmapped")
    if fetch_rows is None and (
        not settings.mysql_source_enabled
        or not (settings.mysql_source_url or "").strip()
    ):
        return empty_key_nodes("source_unavailable", keys)
    try:
        rows = await (fetch_rows or _fetch_rows)(land_key, keys)
    except Exception as exc:  # noqa: BLE001 — 关键节点缺失不能挡住遥感报告
        logger.warning(
            "biweekly_key_nodes_unavailable",
            land_id=str(land_id),
            error_type=type(exc).__name__,
        )
        return empty_key_nodes("query_failed", keys)
    parsed = parse_workbox_rows(list(rows or []))
    status = "ready" if parsed["nodes"] else "empty"
    return {"status": status, "season_keys": keys, **parsed}


async def _fetch_rows(
    land_id: int | str, season_keys: list[str]
) -> list[dict[str, Any]]:
    from app.services.mysql_land_sync import _mysql_engine

    engine = _mysql_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            _SQL, {"land_id": land_id, "season_keys": list(season_keys)}
        )
        rows = [dict(row) for row in result.mappings().all()]
    await engine.dispose()
    return [row for row in rows if str(row.get("land_id") or land_id) == str(land_id)]
