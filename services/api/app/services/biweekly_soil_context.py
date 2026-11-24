"""Short soil background for a biweekly conclusion.

SoilGrids and the vendor NPK snapshot describe the field, not this
fortnight. The download worker does not query Postgres for them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.logging import logger


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def compact_soil(
    npk: dict[str, Any] | None, summary: dict[str, Any] | None
) -> dict[str, Any]:
    """Keep the few fields that can explain water or growth, and drop the vendor blob."""
    body: dict[str, Any] = {}
    npk = npk or {}
    summary = summary or {}
    for key, source in (
        ("ph", npk.get("ph") if npk.get("ph") is not None else summary.get("avg_ph")),
        ("som_g_kg", npk.get("som_g_kg")),
        ("an_mg_kg", npk.get("an_mg_kg")),
        ("ap_mg_kg", npk.get("ap_mg_kg")),
        ("ak_mg_kg", npk.get("ak_mg_kg")),
    ):
        number = _num(source)
        if number is not None:
            body[key] = number
    texture = npk.get("texture_usda_cn") or summary.get("dominant_texture")
    if texture:
        body["texture"] = str(texture)
    drainage = summary.get("drainage_class")
    if drainage:
        body["drainage_class"] = str(drainage)
    awc = _num(summary.get("rootzone_awc_mm"))
    if awc is not None:
        body["rootzone_awc_mm"] = awc
    risk = _num(summary.get("waterlogging_risk"))
    if risk is not None:
        body["waterlogging_risk"] = risk
    body["status"] = "ready" if len(body) else "absent"
    return body


async def load_soil_context(
    land_id: str, *, session: Any | None = None
) -> dict[str, Any]:
    if session is not None:
        return await _read(session, land_id)
    try:
        from app.core.database import async_session
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "biweekly_soil_context_unavailable", error_type=type(exc).__name__
        )
        return {"status": "query_failed"}
    try:
        async with async_session() as opened:
            return await _read(opened, land_id)
    except Exception as exc:  # noqa: BLE001 — 土壤档案缺失不能挡住报告
        logger.warning(
            "biweekly_soil_context_unavailable", error_type=type(exc).__name__
        )
        return {"status": "query_failed"}


async def _read(session: Any, land_id: str) -> dict[str, Any]:
    npk_sql = text(
        """
        SELECT ph, som_g_kg, an_mg_kg, ap_mg_kg, ak_mg_kg, texture_usda_cn
        FROM soil_nutrient_npk
        WHERE land_id = :land_id
        LIMIT 1
        """
    )
    summary_sql = text(
        """
        SELECT dominant_texture, avg_ph, rootzone_awc_mm, drainage_class, waterlogging_risk
        FROM soil_field_summary
        WHERE land_id = :land_id
        LIMIT 1
        """
    )
    npk_row = (
        (await session.execute(npk_sql, {"land_id": str(land_id)})).mappings().first()
    )
    summary_row = (
        (await session.execute(summary_sql, {"land_id": str(land_id)}))
        .mappings()
        .first()
    )
    return compact_soil(
        dict(npk_row) if npk_row else None,
        dict(summary_row) if summary_row else None,
    )
