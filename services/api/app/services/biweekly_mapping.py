"""Wheat S1/S2/S3 mapping: prefer smart_agric.crop_remote_phase_mapping, else pilot.

Never executes DDL/DML. If the table is missing or MySQL is disabled, fall
back to the in-code Smart wheat-name pilot and mark PENDING_AGRONOMY_REVIEW.
Does not invent mappings for other crops.
"""

from __future__ import annotations

from typing import Any

from agric_satellite_analysis_common.biweekly.phase_policy import (
    CROP_ID,
    CROP_NAME,
    MAPPING_VERSION,
    WHEAT_STAGES,
    resolve_wheat_stage,
)
from sqlalchemy import text

from app.core.config import settings

_MISSING_TABLE_MARKERS = (
    "crop_remote_phase_mapping",
    "doesn't exist",
    "does not exist",
    "unknown table",
    "no such table",
)


def _pilot_mapping(*, table_available: bool = False, note: str | None = None) -> dict[str, Any]:
    rows = []
    for key in WHEAT_STAGES:
        resolved = resolve_wheat_stage(key)
        rows.append(
            {
                "crop_id": CROP_ID,
                "crop_name": CROP_NAME,
                "stage_name": key,
                "stage_code": resolved["stage_code"],
                "mapping_status": resolved["status"],
                "phase_code": resolved.get("phase_code"),
                "phase_label": resolved.get("phase_label"),
                "context_tags": ",".join(resolved.get("tags") or ()),
                "review_status": resolved.get("review_status") or "PENDING_AGRONOMY_REVIEW",
                "mapping_version": MAPPING_VERSION,
            }
        )
    return {
        "source": "in_code_pilot",
        "table_available": table_available,
        "mapping_version": MAPPING_VERSION,
        "review_status": "PENDING_AGRONOMY_REVIEW",
        "crop_id": CROP_ID,
        "crop_name": CROP_NAME,
        "rows": rows,
        "note": note
        or "smart_agric.crop_remote_phase_mapping 不可用，使用代码内小麦试点映射（Smart 中文生育期名）。待农学审核。",
    }


def _is_missing_table(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "crop_remote_phase_mapping" in message or any(
        marker in message for marker in ("doesn't exist", "does not exist", "unknown table", "no such table")
    )


async def load_wheat_phase_mapping() -> dict[str, Any]:
    if not settings.mysql_source_enabled or not (settings.mysql_source_url or "").strip():
        return _pilot_mapping()
    try:
        from app.services.mysql_land_sync import _mysql_engine

        engine = _mysql_engine()
        sql = text(
            """
            SELECT crop_id, crop_name, stage_name, mapping_status, phase_code, phase_label,
                   context_tags, review_status, mapping_version
            FROM crop_remote_phase_mapping
            WHERE if_delete = 0
              AND crop_id = :crop_id
              AND mapping_version = :mapping_version
            """
        )
        async with engine.connect() as conn:
            result = await conn.execute(
                sql,
                {"crop_id": CROP_ID, "mapping_version": MAPPING_VERSION},
            )
            rows = [dict(row) for row in result.mappings().all()]
        if not rows:
            return _pilot_mapping(
                table_available=True,
                note="映射表存在但无小麦试点行，回退代码内 Smart 中文名映射。未为其他作物编造映射。",
            )
        review = {str(row.get("review_status") or "PENDING_AGRONOMY_REVIEW") for row in rows}
        return {
            "source": "smart_agric.crop_remote_phase_mapping",
            "table_available": True,
            "mapping_version": MAPPING_VERSION,
            "review_status": "APPROVED" if review == {"APPROVED"} else "PENDING_AGRONOMY_REVIEW",
            "crop_id": CROP_ID,
            "crop_name": CROP_NAME,
            "rows": rows,
            "note": "已读取 Smart 库 crop_remote_phase_mapping。本服务未改写该表。",
        }
    except Exception as exc:  # noqa: BLE001 — missing table or source must not fail the API
        fallback = _pilot_mapping()
        if _is_missing_table(exc):
            fallback["note"] = (
                "smart_agric.crop_remote_phase_mapping 不存在，使用代码内小麦试点映射。待农学审核。"
            )
        return fallback
