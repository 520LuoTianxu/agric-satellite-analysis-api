"""Read wheat planting calendars from Smart (smart_agric) via MYSQL_SOURCE_URL.

A land is in season only when agriculture_batch_task has a live row for that
land. Status 0, 1 and 2 are still planting; 3 and 4 are finished and are not
listed. The land key is agriculture_batch_task.land_id (exact match, never
LIKE). crop_batch_land_scheme is not used, and agriculture_crop_batch.land_id
is not the land key: wheat batches are often stored with land_id=0.

agriculture_plant_plan is referenced by agriculture_plant_task.plan_id
comments, but that table does not exist in smart_agric. plan_id equals
agriculture_crop_batch.batch_id.

Phenology windows still come from agriculture_plant_task. The batch-task
table only decides which batches are currently planting; its farm-work rows
are not turned into stages. Missing plans are returned as an empty,
documented state — never by substituting another land's plan.
"""

from __future__ import annotations

from typing import Any

from agric_satellite_analysis_common.biweekly.models import ReportError
from agric_satellite_analysis_common.biweekly.phase_policy import CROP_ID, PHASE_LABELS

# 种植季与分期的纯计算放在公共包，离线出报工具与本接口共用同一份推导，避免两处各算各的。
from agric_satellite_analysis_common.biweekly.plan_calendar import (
    MISSING_NOTE,
    SmartPlantingCatalog,
    build_seasons_from_rows,
)
from agric_satellite_analysis_common.biweekly.plan_calendar import (
    empty_calendar as _empty,
)
from sqlalchemy import bindparam, text

from app.core.config import settings

__all__ = [
    "MISSING_NOTE",
    "NO_ACTIVE_BATCH_NOTE",
    "PLANTING_TASK_STATUSES",
    "QUERY_FAILED_NOTE",
    "SOURCE_UNAVAILABLE_NOTE",
    "SmartPlantingCatalog",
    "attach_public_stages",
    "build_seasons_from_rows",
    "exact_land_id",
    "load_smart_planting_calendar",
    "public_stage_intervals",
]

SOURCE_UNAVAILABLE_NOTE = (
    "API 机未配置 MYSQL_SOURCE_URL，无法读取 Smart 种植方案。不会改用其他地块的方案。"
)
QUERY_FAILED_NOTE = "读取 Smart 种植方案失败。不会改用其他地块的方案。"
# agriculture_batch_task.status：0、1、2 为种植中，3、4 为已完成。已完成的批次不进入双周报。
PLANTING_TASK_STATUSES = ("0", "1", "2")
NO_ACTIVE_BATCH_NOTE = (
    "本田没有种植中的批次（agriculture_batch_task.status 为 0、1、2）。"
    "已完成（3、4）不列出。不会改用其他地块的方案。"
)


def exact_land_id(land_id: str) -> int | str:
    """Bind an exact land id. Reject wildcard patterns such as %6417%."""
    value = str(land_id or "").strip()
    if not value or any(ch in value for ch in "%_*[]"):
        raise ReportError("LAND_ID_INVALID")
    return int(value) if value.isdigit() else value


def public_stage_intervals(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Stable public phenology windows from a Smart native plan.

    Keeps NOT_APPLICABLE pre-monitoring stages (播种前准备, 播种期) even when
    phase_label is null. Does not invent stages that are not on the plan.
    """
    stages: list[dict[str, Any]] = []
    for row in (plan or {}).get("intervals") or []:
        name = str(row.get("source_stage_name") or row.get("stage_code") or "").strip()
        start = row.get("start")
        end_inclusive = row.get("end_inclusive")
        end_exclusive = row.get("end_exclusive")
        if not name or not start or not end_inclusive:
            continue
        phase_label = row.get("phase_label") or None
        status = str(row.get("mapping_status") or "")
        stages.append(
            {
                "name": name,
                "source_stage_name": name,
                "stage_code": row.get("stage_code"),
                "start": start,
                "end_inclusive": end_inclusive,
                "end_exclusive": end_exclusive,
                "mapping_status": status or None,
                "phase_label": phase_label,
                "phase_code": row.get("phase_code") or None,
                "phase_name": PHASE_LABELS.get(phase_label) if phase_label else None,
                "monitored": status != "NOT_APPLICABLE" and bool(phase_label),
                "tags": list(row.get("tags") or []),
            }
        )
    return stages


def attach_public_stages(
    payload: dict[str, Any], calendar: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Expose `stages` (and native_plan on period lists) for the frontend timeline."""
    calendar_by_id = {
        str(row["season_id"]): row
        for row in (calendar or {}).get("seasons") or []
        if row.get("season_id")
    }
    for season in payload.get("seasons") or []:
        plan = season.get("native_plan") or (
            calendar_by_id.get(str(season.get("season_id"))) or {}
        ).get("native_plan")
        if plan and not season.get("native_plan"):
            season["native_plan"] = plan
        season["stages"] = public_stage_intervals(plan)
    if "periods" in payload:
        season_id = str(payload.get("season_id") or "")
        plan = payload.get("native_plan") or (calendar_by_id.get(season_id) or {}).get(
            "native_plan"
        )
        if plan:
            payload["native_plan"] = plan
        payload["stages"] = public_stage_intervals(plan)
    return payload


async def load_smart_planting_calendar(
    land_id: str,
    *,
    mapping_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if (
        not settings.mysql_source_enabled
        or not (settings.mysql_source_url or "").strip()
    ):
        return _empty(
            land_id=str(land_id),
            plan_status="smart_source_unavailable",
            note=SOURCE_UNAVAILABLE_NOTE,
        )
    try:
        land_key = exact_land_id(land_id)
        from app.services.mysql_land_sync import _mysql_engine

        engine = _mysql_engine()
        # 地块和是否种植中只看 agriculture_batch_task。status 3、4 已完成，不取。
        # 批次作物信息仍来自 agriculture_crop_batch，但不用它的 land_id，也不再走方案地块表。
        batch_sql = text(
            """
            SELECT
              b.batch_id,
              t.land_id,
              b.batch_name,
              b.crop_id,
              b.crop_name,
              b.start_time,
              b.project_crop_season_id,
              b.crop_planting_season_type_id,
              b.scheme_handling_profile,
              b.scheme_id
            FROM (
              SELECT DISTINCT batch_id, land_id
              FROM agriculture_batch_task
              WHERE land_id = :land_id
                AND del_flag = '0'
                AND status IN :planting_statuses
            ) AS t
            INNER JOIN agriculture_crop_batch AS b
              ON b.batch_id = t.batch_id
            WHERE b.del_flag = '0'
              AND b.crop_id = :crop_id
            ORDER BY b.start_time, b.batch_id
            """
        ).bindparams(bindparam("planting_statuses", expanding=True))
        async with engine.connect() as conn:
            batch_result = await conn.execute(
                batch_sql,
                {
                    "land_id": land_key,
                    "crop_id": CROP_ID,
                    "planting_statuses": list(PLANTING_TASK_STATUSES),
                },
            )
            batches = [dict(row) for row in batch_result.mappings().all()]
            # 防止驱动把绑定参数放宽成模糊匹配，只留下本次请求的地块。
            batches = [
                row for row in batches if str(row.get("land_id")) == str(land_key)
            ]
            if not batches:
                return _empty(
                    land_id=str(land_key),
                    plan_status="missing",
                    note=NO_ACTIVE_BATCH_NOTE,
                )
            plan_ids = [row["batch_id"] for row in batches]
            task_sql = text(
                """
                SELECT
                  plant_task_id, plan_id, task_name, external_cycle_id, job_id,
                  plan_start, plan_finish, species_name, component_crop_id,
                  del_flag, order_num
                FROM agriculture_plant_task
                WHERE plan_id IN :plan_ids
                  AND (del_flag = '0' OR del_flag IS NULL)
                ORDER BY plan_id, plan_start, order_num, plant_task_id
                """
            ).bindparams(bindparam("plan_ids", expanding=True))
            task_result = await conn.execute(task_sql, {"plan_ids": list(plan_ids)})
            tasks = [dict(row) for row in task_result.mappings().all()]
        return build_seasons_from_rows(
            str(land_key), batches, tasks, mapping_rows=mapping_rows
        )
    except ReportError:
        raise
    except Exception:  # noqa: BLE001 — plan lookup must not crash the API process
        return _empty(
            land_id=str(land_id), plan_status="query_failed", note=QUERY_FAILED_NOTE
        )
