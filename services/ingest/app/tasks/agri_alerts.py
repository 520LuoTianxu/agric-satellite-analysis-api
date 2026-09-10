"""Evaluate RS alerts from agri.parcel_scene_products (lonlat_v1).

Classic COG pipeline calls ``run_alerts`` after FieldStat. Agri-tagged fields
skip that path, so we re-run threshold/drop rules from S2 scene averages here —
after lonlat upsert (bridge) and on explicit refresh.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select, text

from app.tasks.indices import INDEX_REGISTRY, IndexDef
from app.tasks.pipeline import _get_weather_context, get_db_session
from app.worker import celery_app

logger = structlog.get_logger()

# parcel_scene_products average columns ↔ IndexDef.key
AGRI_AVG_COLUMNS: dict[str, str] = {
    "ndvi": "ndvi_avg",
    "evi": "evi_avg",
    "ndmi": "ndmi_avg",
    "ndre": "ndre_avg",
    "cire": "cire_avg",
    "mndwi": "mndwi_avg",
}

# Primary stress indices evaluated on refresh / ingest (avoid alert floods).
DEFAULT_INDEX_KEYS: tuple[str, ...] = ("ndvi", "evi", "ndmi")


def _load_field_meta(session, field_id: str) -> dict[str, Any] | None:
    from app.core.agri_tags import parse_agri_land_id
    from app.models.tables import Field

    field = session.get(Field, uuid.UUID(field_id))
    if not field or field.deleted_at is not None:
        return None
    land_id = parse_agri_land_id(field.tags_json)
    return {
        "field_id": field.id,

        "land_id": land_id,
        "tags": field.tags_json,
    }


def _load_s2_series(session, land_id: str) -> list[dict[str, Any]]:
    """Return S2 scenes for land_id ordered by date ascending."""
    rows = (
        session.execute(
            text(
                """
            SELECT date, cloud_cover, cloud_cover_over_30, parcel_cloud_cover_pct,
                   ndvi_avg, evi_avg, ndmi_avg, ndre_avg, cire_avg, mndwi_avg
            FROM agri.parcel_scene_products
            WHERE land_id = :lid AND sensor = 'S2'
            ORDER BY date ASC
            """
            ),
            {"lid": str(land_id)})
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def _is_clear(scene: dict[str, Any]) -> bool:
    if scene.get("cloud_cover_over_30") is False:
        return True
    if scene.get("cloud_cover_over_30") is True:
        return False
    cc = scene.get("cloud_cover")
    if isinstance(cc, (int, float)):
        return float(cc) <= 30
    pct = scene.get("parcel_cloud_cover_pct")
    if isinstance(pct, (int, float)):
        return float(pct) <= 30
    return False


def _pick_eval_scene(
    series: list[dict[str, Any]],
    *,
    scene_date: date | None,
    avg_col: str) -> dict[str, Any] | None:
    usable = [
        s
        for s in series
        if s.get(avg_col) is not None and isinstance(s.get(avg_col), (int, float))
    ]
    if not usable:
        return None
    if scene_date is not None:
        for s in usable:
            if s["date"] == scene_date:
                return s
        return None
    # Prefer latest clear scene; fallback to latest with a value.
    clear = [s for s in usable if _is_clear(s)]
    return (clear or usable)[-1]


def _delete_open_rs_alerts(session, field_id, index_keys: list[str]) -> int:
    from app.models.tables import Alert

    rule_names: list[str] = []
    for key in index_keys:
        rule_names.append(f"{key}_threshold")
        rule_names.append(f"{key}_drop")
    result = session.execute(
        select(Alert).where(
            Alert.field_id == field_id,
            Alert.status == "open",
            Alert.rule_name.in_(rule_names))
    )
    rows = result.scalars().all()
    for row in rows:
        session.delete(row)
    return len(rows)


def _existing_alert_keys(session, field_id) -> set[tuple[date, str]]:
    from app.models.tables import Alert

    rows = session.execute(
        select(Alert.date, Alert.rule_name).where(Alert.field_id == field_id)
    ).all()
    return {(r[0], r[1]) for r in rows}


def _emit_rules(
    session,
    *,
    field_id,
    scene_date: date,
    current_mean: float,
    historical_means: list[float],
    index_def: IndexDef,
    weather_ctx: dict | None,
    existing: set[tuple[date, str]]) -> int:
    from app.models.tables import Alert

    created = 0
    alert_cfg = index_def.alerts
    label = index_def.label

    if current_mean < alert_cfg.threshold:
        rule = f"{index_def.key}_threshold"
        if (scene_date, rule) not in existing:
            severity = "high" if current_mean < alert_cfg.threshold_high else "medium"
            session.add(
                Alert(

                    field_id=field_id,
                    date=scene_date,
                    severity=severity,
                    rule_name=rule,
                    rule_params_json={
                        "threshold": alert_cfg.threshold,
                        "source": "agri",
                    },
                    message=(
                        f"{label} mean ({current_mean:.3f}) below threshold "
                        f"({alert_cfg.threshold}). Consider scouting."
                    ),
                    status="open",
                    index_type=index_def.key,
                    weather_context=weather_ctx)
            )
            existing.add((scene_date, rule))
            created += 1

    if len(historical_means) >= 2:
        window = historical_means[-alert_cfg.drop_window :]
        rolling_avg = sum(window) / len(window)
        if rolling_avg > 0:
            drop_pct = ((rolling_avg - current_mean) / rolling_avg) * 100
            if drop_pct >= alert_cfg.drop_pct:
                rule = f"{index_def.key}_drop"
                if (scene_date, rule) not in existing:
                    severity = (
                        "high"
                        if drop_pct >= 30
                        else "medium"
                        if drop_pct >= 20
                        else "low"
                    )
                    session.add(
                        Alert(

                            field_id=field_id,
                            date=scene_date,
                            severity=severity,
                            rule_name=rule,
                            rule_params_json={
                                "drop_pct": alert_cfg.drop_pct,
                                "window": alert_cfg.drop_window,
                                "source": "agri",
                            },
                            message=(
                                f"{label} dropped {drop_pct:.1f}% "
                                f"(from avg {rolling_avg:.3f} to {current_mean:.3f}). "
                                f"Investigate crop stress."
                            ),
                            status="open",
                            index_type=index_def.key,
                            weather_context=weather_ctx)
                    )
                    existing.add((scene_date, rule))
                    created += 1
    return created


def evaluate_agri_rs_alerts_for_field(
    field_id: str,
    *,
    land_id: str | None = None,
    scene_date: date | str | None = None,
    index_keys: list[str] | None = None,
    replace_open: bool = True) -> dict[str, Any]:
    """Evaluate threshold/drop alerts from agri S2 averages.

    Default behaviour (refresh / post-bridge): replace open optical RS alerts and
    evaluate the latest clear scene per index against full history.

    When ``scene_date`` is set (single-date ingest), only that date is evaluated
    and existing alerts for other dates are left alone unless ``replace_open``.
    """
    session = get_db_session()
    try:
        meta = _load_field_meta(session, field_id)
        if not meta:
            return {
                "field_id": field_id,
                "status": "error",
                "detail": "Field not found",
            }
        lid = land_id or meta["land_id"]
        if not lid:
            return {
                "field_id": field_id,
                "status": "skipped",
                "reason": "not_agri_tagged",
            }

        keys = list(index_keys or DEFAULT_INDEX_KEYS)
        keys = [k for k in keys if k in AGRI_AVG_COLUMNS and k in INDEX_REGISTRY]
        if not keys:
            return {"field_id": field_id, "status": "skipped", "reason": "no_indices"}

        series = _load_s2_series(session, lid)
        if not series:
            return {
                "field_id": field_id,
                "land_id": lid,
                "status": "skipped",
                "reason": "no_s2_scenes",
            }

        target_date: date | None
        if scene_date is None:
            target_date = None
        elif isinstance(scene_date, date):
            target_date = scene_date
        else:
            target_date = date.fromisoformat(str(scene_date)[:10])

        removed = 0
        if replace_open:
            removed = _delete_open_rs_alerts(session, meta["field_id"], keys)

        existing = _existing_alert_keys(session, meta["field_id"])
        created = 0
        evaluated: list[dict[str, Any]] = []

        for key in keys:
            avg_col = AGRI_AVG_COLUMNS[key]
            index_def = INDEX_REGISTRY[key]
            scene = _pick_eval_scene(series, scene_date=target_date, avg_col=avg_col)
            if not scene:
                continue
            sd: date = scene["date"]
            # History up to and excluding current (then include current like pipeline)
            hist = [
                float(s[avg_col])
                for s in series
                if s["date"] < sd
                and s.get(avg_col) is not None
                and isinstance(s.get(avg_col), (int, float))
            ]
            mean = float(scene[avg_col])
            hist.append(mean)
            weather_ctx = _get_weather_context(session, meta["field_id"], sd)
            n = _emit_rules(
                session,

                field_id=meta["field_id"],
                scene_date=sd,
                current_mean=mean,
                historical_means=hist,
                index_def=index_def,
                weather_ctx=weather_ctx,
                existing=existing)
            created += n
            evaluated.append(
                {
                    "index": key,
                    "date": sd.isoformat(),
                    "mean": round(mean, 4),
                    "alerts_created": n,
                }
            )

        session.commit()
        result = {
            "field_id": field_id,
            "land_id": lid,
            "status": "ok",
            "removed_open": removed,
            "created": created,
            "evaluated": evaluated,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            "agri_rs_alerts_evaluated",
            **{
                k: result[k] for k in ("field_id", "land_id", "created", "removed_open")
            })
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(
    name="app.tasks.agri_alerts.evaluate_agri_alerts_for_field",
    bind=True,
    max_retries=2,
    time_limit=300,
    soft_time_limit=240)
def evaluate_agri_alerts_for_field(
    self,
    field_id: str,
    land_id: str | None = None,
    scene_date: str | None = None,
    replace_open: bool = True) -> dict:
    """Celery entry: agri lonlat → openfarm alerts."""
    try:
        return evaluate_agri_rs_alerts_for_field(
            field_id,
            land_id=land_id,
            scene_date=scene_date,
            replace_open=replace_open)
    except Exception as e:
        logger.error(
            "agri_rs_alerts_failed",
            field_id=field_id,
            error=str(e))
        raise
