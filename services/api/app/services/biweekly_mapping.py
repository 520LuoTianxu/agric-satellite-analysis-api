"""Wheat S1/S2/S3 mapping: prefer prec_agric.crop_remote_phase_mapping, else pilot.

Never executes DDL/DML. If the table is missing or MySQL is disabled, fall
back to the in-code QX/T 88-2008 pilot and mark PENDING_AGRONOMY_REVIEW.
"""

from __future__ import annotations

from typing import Any

from agric_satellite_analysis_common.biweekly.phase_policy import (
    MAPPING_VERSION,
    WHEAT_STAGES,
    resolve_wheat_stage,
)
from sqlalchemy import text

from app.core.config import settings

WHEAT_SPECIES_CODE = 2000000005002


def _pilot_mapping() -> dict[str, Any]:
    rows = []
    for key, raw in WHEAT_STAGES.items():
        resolved = resolve_wheat_stage(key)
        rows.append(
            {
                "stage_code": resolved["stage_code"],
                "stage_key": key,
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
        "table_available": False,
        "mapping_version": MAPPING_VERSION,
        "review_status": "PENDING_AGRONOMY_REVIEW",
        "species_code": WHEAT_SPECIES_CODE,
        "rows": rows,
        "note": "prec_agric.crop_remote_phase_mapping 不可用，使用代码内小麦试点映射。待农学审核。",
    }


async def load_wheat_phase_mapping() -> dict[str, Any]:
    if not settings.mysql_source_enabled or not (settings.mysql_source_url or "").strip():
        return _pilot_mapping()
    try:
        from app.services.mysql_land_sync import _mysql_engine

        engine = _mysql_engine()
        sql = text(
            """
            SELECT stage_code, mapping_status, phase_code, phase_label,
                   context_tags, review_status, mapping_version
            FROM crop_remote_phase_mapping
            WHERE if_delete = 0
              AND species_code = :species_code
              AND mapping_version = :mapping_version
            """
        )
        async with engine.connect() as conn:
            result = await conn.execute(
                sql,
                {"species_code": WHEAT_SPECIES_CODE, "mapping_version": MAPPING_VERSION},
            )
            rows = [dict(row) for row in result.mappings().all()]
        if not rows:
            fallback = _pilot_mapping()
            fallback["table_available"] = True
            fallback["note"] = "映射表存在但无小麦试点行，回退代码内映射。"
            return fallback
        review = {str(row.get("review_status") or "PENDING_AGRONOMY_REVIEW") for row in rows}
        return {
            "source": "prec_agric.crop_remote_phase_mapping",
            "table_available": True,
            "mapping_version": MAPPING_VERSION,
            "review_status": "APPROVED" if review == {"APPROVED"} else "PENDING_AGRONOMY_REVIEW",
            "species_code": WHEAT_SPECIES_CODE,
            "rows": rows,
            "note": "已读取用户自行执行的 mapping 表，本服务未改写该表。",
        }
    except Exception:  # noqa: BLE001 — missing table or source must not fail the API
        return _pilot_mapping()
