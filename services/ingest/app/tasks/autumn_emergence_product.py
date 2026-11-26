"""Download-machine autumn emergence on official S2 parcel products.

After an official S2 product is built, this module writes that day's
per-pixel emergence result into the same official JSON, uploads it, and
lets the existing result callback notify the API. The API does not compute
pixels and does not mkdir /data for this.

A day's result is always analyze(stacks from sowing through that day).
If a later backfill inserts an earlier official scene, later unfrozen
dates are recomputed. Once the field is frozen as emerged, compute stops.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import date, datetime
from typing import Any

from agric_satellite_analysis_common.autumn_emergence import (
    DAILY_FIELD_KEY,
    METHOD_VERSION,
    analyze,
    attach_daily_field,
    autumn_season_key,
    daily_field_of,
    default_season_key,
    is_winter_wheat,
    official_stack_from_product,
    season_bounds,
    stack_from_pixel_data,
)
from agric_satellite_analysis_common.mq_results import (
    scene_json_oss_key,
    upload_scene_product_json,
)

logger = logging.getLogger(__name__)

PULL_PURPOSE = "autumn_emergence"

_batch_ctx: ContextVar[dict[str, Any] | None] = ContextVar(
    "autumn_emergence_batch", default=None
)


def is_emergence_job(params: dict[str, Any] | None) -> bool:
    if not isinstance(params, dict):
        return False
    if params.get("autumn_emergence_manual") in {True, "true", "1", 1}:
        return True
    return str(params.get("purpose") or params.get("display_type") or "") == PULL_PURPOSE


def set_batch_context(
    params: dict[str, Any] | None,
    *,
    crop_type: str | None = None,
    crop_by_land: dict[str, str | None] | None = None,
):
    ctx = {
        "params": params if isinstance(params, dict) else {},
        "crop_type": crop_type,
        "crop_by_land": crop_by_land or {},
        "manual": is_emergence_job(params),
    }
    return _batch_ctx.set(ctx)


def update_batch_crops(crop_by_land: dict[str, str | None]) -> None:
    ctx = dict(current_batch_context())
    merged = dict(ctx.get("crop_by_land") or {})
    merged.update(crop_by_land)
    ctx["crop_by_land"] = merged
    _batch_ctx.set(ctx)


def reset_batch_context(token) -> None:
    try:
        _batch_ctx.reset(token)
    except Exception:
        _batch_ctx.set(None)


def current_batch_context() -> dict[str, Any]:
    return _batch_ctx.get() or {}


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _bool(value: Any) -> bool:
    return value in {True, "true", "1", 1}


def should_compute(
    *,
    day: date | None,
    crop_type: str | None,
    manual: bool,
    frozen_season: str | None,
    emerged: bool,
    sowing_date: date | None,
) -> bool:
    if day is None:
        return False
    season = autumn_season_key(day)
    if season is None:
        return False
    if emerged and frozen_season == season:
        return False
    if sowing_date is not None and day < sowing_date:
        return False
    if manual:
        return True
    return is_winter_wheat(crop_type)


def enrich_official_product(product: dict[str, Any]) -> dict[str, Any] | None:
    """Attach today's emergence field and recompute later unfrozen dates.

    Returns the current product (possibly enriched) so the caller can upload
    it. Later dates that changed are uploaded and notified here.
    On skip or error, returns the original product unchanged.
    """
    try:
        return _enrich_official_product(product)
    except Exception:
        logger.exception(
            "autumn_emergence_attach_failed",
            extra={"land_id": (product or {}).get("land_id")},
        )
        return product


def _enrich_official_product(product: dict[str, Any]) -> dict[str, Any]:
    stack, _total = official_stack_from_product(product)
    if stack is None:
        return product
    land_id = str(product.get("land_id") or "")
    if not land_id:
        return product
    ctx = current_batch_context()
    params = ctx.get("params") or {}
    manual = bool(ctx.get("manual"))
    crops = ctx.get("crop_by_land") or {}
    crop_type = crops.get(land_id, ctx.get("crop_type"))
    meta = _land_context(land_id)
    if crop_type is None:
        crop_type = meta.get("crop_type")
    sowing = _as_date(params.get("sowing_date")) or _sowing_for_season(
        meta, stack.day
    )
    season = autumn_season_key(stack.day)
    if not should_compute(
        day=stack.day,
        crop_type=crop_type,
        manual=manual,
        frozen_season=meta.get("autumn_emerged_season"),
        emerged=_bool(meta.get("autumn_emerged")),
        sowing_date=sowing,
    ):
        return product
    start, end = season_bounds(season or default_season_key(stack.day))
    if sowing is not None:
        start = max(start, sowing)
    window = _load_window_products(land_id, start, end, current=product)
    changed = _compute_window(land_id, window, sowing=sowing)
    current_date = stack.day.isoformat()
    current = window.get(current_date) or product
    for day_s, updated in changed.items():
        if day_s == current_date:
            continue
        _republish(updated)
    return current


def finalize_emergence_job(params: dict[str, Any] | None) -> dict[str, Any]:
    """After an 出苗 pull, fill daily fields even when existing scenes were skipped."""
    if not is_emergence_job(params):
        return {"action": "skipped"}
    params = params or {}
    land_id = str(params.get("land_id") or params.get("job_land_id") or "").strip()
    sowing = _as_date(params.get("sowing_date"))
    date_from = _as_date(params.get("date_from")) or sowing
    date_to = _as_date(params.get("date_to"))
    season = str(params.get("season_key") or "") or (
        autumn_season_key(sowing or date_from or date.today())
        or default_season_key(date.today())
    )
    if not land_id:
        return {"action": "skipped_no_land"}
    try:
        return _finalize_land(
            land_id,
            season=season,
            sowing=sowing,
            date_from=date_from,
            date_to=date_to,
            manual=True,
        )
    except Exception:
        logger.exception("autumn_emergence_finalize_failed", extra={"land_id": land_id})
        return {"action": "error", "land_id": land_id}


def _finalize_land(
    land_id: str,
    *,
    season: str,
    sowing: date | None,
    date_from: date | None,
    date_to: date | None,
    manual: bool,
    crop_type: str | None = None,
) -> dict[str, Any]:
    meta = _land_context(land_id)
    if crop_type is None:
        crop_type = meta.get("crop_type")
    start, end = season_bounds(season)
    if sowing is None:
        sowing = _sowing_for_season(meta, start)
    if sowing is not None:
        start = max(start, sowing)
    if date_from is not None:
        start = max(start, date_from)
    if date_to is not None:
        end = min(end, date_to)
    if start > end:
        return {"action": "skipped_window", "land_id": land_id}
    # Manual finalize always computes; automatic path already filtered.
    if _bool(meta.get("autumn_emerged")) and meta.get("autumn_emerged_season") == season:
        return {"action": "skipped_frozen", "land_id": land_id, "season_key": season}
    if not manual and not is_winter_wheat(crop_type):
        return {"action": "skipped_ineligible", "land_id": land_id}
    window = _load_window_products(land_id, start, end, current=None)
    changed = _compute_window(land_id, window, sowing=sowing)
    for updated in changed.values():
        _republish(updated)
    return {
        "action": "updated" if changed else "noop",
        "land_id": land_id,
        "season_key": season,
        "dates": list(changed.keys()),
        "method_version": METHOD_VERSION,
    }


def _compute_window(
    land_id: str,
    products: dict[str, dict[str, Any]],
    *,
    sowing: date | None,
) -> dict[str, dict[str, Any]]:
    """Write each official date's field from stacks through that day. Stop once emerged."""
    stacks = []
    changed: dict[str, dict[str, Any]] = {}
    for day_s in sorted(products):
        day = _as_date(day_s)
        if day is None:
            continue
        if sowing is not None and day < sowing:
            continue
        stack, _total = official_stack_from_product(products[day_s])
        if stack is None:
            continue
        stacks.append(stack)
        result = analyze(stacks)
        field = result.to_daily_field(land_id, sowing_date=sowing)
        previous = daily_field_of(products[day_s])
        if previous != {k: v for k, v in field.items()} and previous != field:
            attach_daily_field(products[day_s], field)
            changed[day_s] = products[day_s]
        else:
            # Ensure the in-memory current product still carries the field.
            attach_daily_field(products[day_s], field)
            if previous is None:
                changed[day_s] = products[day_s]
        if result.emerged_flag:
            break
    return changed


def _sowing_for_season(meta: dict[str, Any], day: date) -> date | None:
    season = autumn_season_key(day) or default_season_key(day)
    stored_season = meta.get("autumn_sowing_season")
    sowing = _as_date(meta.get("autumn_sowing_date"))
    if sowing is not None and (not stored_season or stored_season == season):
        return sowing
    return None


def _land_context(land_id: str) -> dict[str, Any]:
    try:
        from agric_satellite_analysis_common.internal_api import (
            autumn_emergence_context,
            internal_api_enabled,
        )

        if internal_api_enabled():
            data = autumn_emergence_context(land_id)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        logger.info(
            "autumn_emergence_context_unavailable",
            extra={"land_id": land_id, "error": str(exc)},
        )
    return {}


def _scene_dates(land_id: str) -> list[str]:
    ctx = _land_context(land_id)
    dates = ctx.get("official_s2_dates") or ctx.get("dates") or []
    if dates:
        return [str(d)[:10] for d in dates]
    try:
        from agric_satellite_analysis_common.internal_api import (
            agri_scene_dates,
            internal_api_enabled,
        )

        if internal_api_enabled():
            return [str(d)[:10] for d in agri_scene_dates(land_id, sensor="S2")]
    except Exception as exc:
        logger.info(
            "autumn_emergence_scene_dates_unavailable",
            extra={"land_id": land_id, "error": str(exc)},
        )
    return []


def _load_window_products(
    land_id: str,
    start: date,
    end: date,
    *,
    current: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    current_day = None
    if current is not None:
        current_day = _as_date(current.get("date"))
        if current_day is not None:
            products[current_day.isoformat()] = current
    for raw in _scene_dates(land_id):
        day = _as_date(raw)
        if day is None or day < start or day > end:
            continue
        if current_day is not None and day == current_day:
            continue
        loaded = _load_oss_product(land_id, day)
        if loaded is not None:
            products[day.isoformat()] = loaded
    return products


def _load_oss_product(land_id: str, day: date) -> dict[str, Any] | None:
    try:
        from agric_satellite_analysis_common.storage import get_storage

        key = scene_json_oss_key(land_id, day.isoformat(), "S2")
        raw = get_storage().get_bytes(key)
        product = json.loads(raw)
        if isinstance(product, dict):
            product.setdefault("land_id", land_id)
            product.setdefault("date", day.isoformat())
            product.setdefault("json_oss_key", key)
            return product
    except Exception as exc:
        logger.info(
            "autumn_emergence_oss_product_missing",
            extra={"land_id": land_id, "date": day.isoformat(), "error": str(exc)},
        )
    return None


def _republish(product: dict[str, Any]) -> None:
    land_id = str(product.get("land_id") or "")
    day = str(product.get("date") or "")[:10]
    if not land_id or not day:
        return
    key, json_url = upload_scene_product_json(
        land_id=land_id,
        date_str=day,
        sensor="S2",
        product=product,
    )
    product["json_oss_key"] = key
    extras = {
        "kind": "parcel_scene_product",
        "land_id": land_id,
        "sensor": "S2",
        "date": day,
        "scene_id": product.get("scene_id"),
        "json_oss_key": key,
        "source": product.get("source"),
        DAILY_FIELD_KEY: True,
    }
    try:
        from app.core.http_mode import cache_scene_result_http, ingest_http_only

        if ingest_http_only():
            cache_scene_result_http(
                label=f"{day}_S2",
                json_url=json_url,
                extras=extras,
            )
            return
    except Exception:
        logger.exception(
            "autumn_emergence_republish_http_failed",
            extra={"land_id": land_id, "date": day},
        )
        raise


__all__ = [
    "PULL_PURPOSE",
    "current_batch_context",
    "enrich_official_product",
    "finalize_emergence_job",
    "is_emergence_job",
    "reset_batch_context",
    "set_batch_context",
    "should_compute",
    "update_batch_crops",
]
