"""按最新有效遥感观测生成农情预警；数据库读写只在 API 机器执行。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.core.agri_classify import (
    CLOUD_MAX_PCT,
    PHENOLOGY_MONTHS,
    WEAK_NDVI_LT,
    classify_drought_series,
    classify_flood_series,
    is_drought_day_class,
    is_official_optical_product,
)
from app.core.crops import get_crop_season
from app.core.database_sync import SyncSession
from app.models.tables import Alert, LandParcel, WeatherDaily
from app.tasks.indices import INDEX_REGISTRY

DEFAULT_INDEX_KEYS = ("ndvi", "evi", "ndmi")
INDEX_COLUMNS = {"ndvi": "ndvi_avg", "evi": "evi_avg", "ndmi": "ndmi_avg"}
OPTICAL_RULES = {
    *(f"{key}_drop" for key in DEFAULT_INDEX_KEYS),
    *(f"{key}_threshold" for key in DEFAULT_INDEX_KEYS),
    "weak_growth",
    "drought_risk",
}
FLOOD_RULES = {"flood_risk"}


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _weather_context(session, land_id: str, alert_date: date) -> dict[str, Any] | None:
    """附带最近一周墒情，便于区分卫星异常与水分胁迫。"""
    start = alert_date - timedelta(days=7)
    rows = (
        session.execute(
            select(WeatherDaily)
            .where(
                WeatherDaily.land_id == land_id,
                WeatherDaily.date >= start,
                WeatherDaily.date <= alert_date,
            )
            .order_by(WeatherDaily.date.desc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return None

    latest = rows[0]
    context: dict[str, Any] = {
        "period": f"{start.isoformat()} to {alert_date.isoformat()}",
        "precipitation_7d_mm": round(sum(float(row.precipitation_sum or 0) for row in rows), 1),
        "et0_7d_mm": round(sum(float(row.et0_fao_mm or 0) for row in rows), 1),
    }
    for column, key, digits in (
        ("water_balance_30d_mm", "water_deficit_mm", 1),
        ("soil_moisture_0_1cm", "soil_moisture_top", 3),
        ("gdd_cumulative", "gdd_cumulative", 1),
        ("drought_index", "drought_index", 2),
    ):
        value = getattr(latest, column)
        if value is not None:
            context[key] = round(float(value), digits)
    return context


def _latest_weather_row(session, land_id: str, as_of: date) -> WeatherDaily | None:
    """读取一周内最新天气指标，用日更墒情补足卫星重访间隔。"""
    return (
        session.execute(
            select(WeatherDaily)
            .where(
                WeatherDaily.land_id == land_id,
                WeatherDaily.date >= as_of - timedelta(days=7),
                WeatherDaily.date <= as_of,
                WeatherDaily.drought_index.is_not(None),
            )
            .order_by(WeatherDaily.date.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _create_weather_drought_alert(
    session,
    *,
    land_id: str,
    weather_row: WeatherDaily | None,
    existing: dict[tuple[date, str], Alert | None],
    active: set[tuple[date, str]],
) -> int:
    """天气日数据出现明显干旱指数时及时预警，不等待下一次晴空卫星过境。"""
    if weather_row is None or weather_row.drought_index is None:
        return 0
    drought_index = float(weather_row.drought_index)
    if drought_index > -1.5:
        return 0
    severity = "high" if drought_index <= -2.5 else "medium"
    return int(
        _add_alert(
            session,
            land_id=land_id,
            alert_date=weather_row.date,
            rule_name="drought_risk",
            severity=severity,
            message=(
                f"近期天气水分平衡显示干旱风险（干旱指数 {drought_index:.2f}）；"
                "建议核查土壤墒情并及时灌溉。"
            ),
            index_type="ndmi",
            params={
                "source": "weather_daily",
                "drought_index": round(drought_index, 3),
                "threshold": -1.5,
            },
            weather_context=_weather_context(session, land_id, weather_row.date),
            existing=existing,
            active=active,
        )
    )


def _load_optical_rows(session, land_id: str, as_of: date) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            text(
                """
                SELECT date, scene_id, product_source, decloud_quality,
                       parcel_cloud_cover_pct, parcel_cloud_source, cloud_cover,
                       cloud_cover_over_30, ndvi_avg, evi_avg, ndmi_avg
                FROM agric_satellite.parcel_scene_products
                WHERE land_id = :land_id AND sensor = 'S2'
                  AND date BETWEEN :start_date AND :as_of
                ORDER BY date, CASE WHEN COALESCE(scene_id, '') LIKE '%_decloud' THEN 1 ELSE 0 END,
                         scene_id
                """
            ),
            {
                "land_id": land_id,
                "start_date": as_of - timedelta(days=365 * 3),
                "as_of": as_of,
            },
        )
        .mappings()
        .all()
    )

    # 同一天原始景和去云景只保留一份官方观测，避免把同日产品误作长势变化。
    by_date: dict[date, dict[str, Any]] = {}
    for raw in rows:
        scene_date = _date(raw.get("date"))
        if scene_date is None:
            continue
        row = dict(raw)
        if not is_official_optical_product(
            source=row.get("product_source"),
            scene_id=row.get("scene_id"),
            decloud_quality=row.get("decloud_quality"),
            parcel_cloud_cover_pct=_number(row.get("parcel_cloud_cover_pct")),
            parcel_cloud_source=row.get("parcel_cloud_source"),
            cloud_cover=_number(row.get("cloud_cover")),
            cloud_cover_over_30=row.get("cloud_cover_over_30"),
            cloud_max_pct=CLOUD_MAX_PCT,
        ):
            continue
        row["date"] = scene_date
        current = by_date.get(scene_date)
        if current is None or (
            current.get("scene_id", "").endswith("_decloud")
            and not str(row.get("scene_id") or "").endswith("_decloud")
        ):
            by_date[scene_date] = row
    return [by_date[key] for key in sorted(by_date)]


def _load_sar_rows(session, land_id: str, as_of: date) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            text(
                """
                SELECT date, scene_id, vv_avg, vh_avg,
                       pixel_data->>'relative_orbit' AS relative_orbit
                FROM agric_satellite.parcel_scene_products
                WHERE land_id = :land_id AND sensor = 'S1'
                  AND date BETWEEN :start_date AND :as_of
                  AND vv_avg IS NOT NULL
                ORDER BY date, scene_id
                """
            ),
            {
                "land_id": land_id,
                "start_date": as_of - timedelta(days=365 * 3),
                "as_of": as_of,
            },
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _add_alert(
    session,
    *,
    land_id: str,
    alert_date: date,
    rule_name: str,
    severity: str,
    message: str,
    index_type: str | None,
    params: dict[str, Any],
    weather_context: dict[str, Any] | None,
    existing: dict[tuple[date, str], Alert | None],
    active: set[tuple[date, str]],
) -> bool:
    key = (alert_date, rule_name)
    active.add(key)
    # 已存在的同日记录保留 ID；同日重算不会清除用户已读状态，也不会重开已关闭项。
    if key in existing:
        return False
    session.add(
        Alert(
            land_id=land_id,
            date=alert_date,
            severity=severity,
            rule_name=rule_name,
            rule_params_json=params,
            message=message,
            status="open",
            index_type=index_type,
            weather_context=weather_context,
        )
    )
    existing[key] = None  # 本次评估中去重，flush 时由数据库分配 ID。
    return True


def _create_optical_alerts(
    session,
    *,
    land_id: str,
    rows: list[dict[str, Any]],
    crop_type: str | None,
    existing: dict[tuple[date, str], Alert],
    active: set[tuple[date, str]],
) -> tuple[int, list[dict[str, Any]]]:
    if not rows:
        return 0, []

    season = get_crop_season(crop_type)
    season_months = sorted(season.season_months or PHENOLOGY_MONTHS)
    latest = rows[-1]
    latest_date = latest["date"]
    latest_month_in_season = latest_date.month in season_months
    created = 0
    evaluated: list[dict[str, Any]] = []
    weather = _weather_context(session, land_id, latest_date)

    if latest_month_in_season:
        ndvi = _number(latest.get("ndvi_avg"))
        if ndvi is not None and ndvi < WEAK_NDVI_LT:
            severity = "high" if ndvi < 0.15 else "medium"
            created += _add_alert(
                session,
                land_id=land_id,
                alert_date=latest_date,
                rule_name="weak_growth",
                severity=severity,
                message=(
                    f"生长季 NDVI 为 {ndvi:.3f}，低于长势关注阈值 {WEAK_NDVI_LT:.2f}；"
                    "建议核查作物覆盖、播种情况和水肥管理。"
                ),
                index_type="ndvi",
                params={
                    "ndvi": round(ndvi, 4),
                    "threshold": WEAK_NDVI_LT,
                    "crop_type": crop_type,
                    "season_months": season_months,
                },
                weather_context=weather,
                existing=existing,
                active=active,
            )

        # 长势、指数下降只在作物生长季评估，避免收获后裸地被反复报成长势异常。
        for key in DEFAULT_INDEX_KEYS:
            value = _number(latest.get(INDEX_COLUMNS[key]))
            if value is None:
                continue
            history = [
                number
                for row in rows[:-1]
                if row["date"].month in season_months
                and (number := _number(row.get(INDEX_COLUMNS[key]))) is not None
            ]
            index_def = INDEX_REGISTRY[key]
            threshold = WEAK_NDVI_LT if key == "ndvi" else index_def.alerts.threshold
            threshold_rule = "weak_growth" if key == "ndvi" else f"{key}_threshold"
            if key != "ndvi" and value < threshold:
                high_threshold = index_def.alerts.threshold_high
                severity = "high" if value < high_threshold else "medium"
                created += _add_alert(
                    session,
                    land_id=land_id,
                    alert_date=latest_date,
                    rule_name=threshold_rule,
                    severity=severity,
                    message=f"{key.upper()} 为 {value:.3f}，低于关注阈值 {threshold:.2f}，建议检查作物水分与长势。",
                    index_type=key,
                    params={"value": round(value, 4), "threshold": threshold},
                    weather_context=weather,
                    existing=existing,
                    active=active,
                )

            window = history[-index_def.alerts.drop_window :]
            if len(window) >= 2:
                average = sum(window) / len(window)
                drop_pct = ((average - value) / average) * 100 if average > 0 else 0
                if drop_pct >= index_def.alerts.drop_pct:
                    severity = "high" if drop_pct >= 30 else "medium" if drop_pct >= 20 else "low"
                    created += _add_alert(
                        session,
                        land_id=land_id,
                        alert_date=latest_date,
                        rule_name=f"{key}_drop",
                        severity=severity,
                        message=f"{key.upper()} 较此前生长季观测均值下降 {drop_pct:.1f}%，建议核查田间胁迫。",
                        index_type=key,
                        params={
                            "value": round(value, 4),
                            "reference_mean": round(average, 4),
                            "drop_pct": round(drop_pct, 1),
                            "threshold_pct": index_def.alerts.drop_pct,
                        },
                        weather_context=weather,
                        existing=existing,
                        active=active,
                    )

    observations = [
        {
            "date": row["date"].isoformat(),
            "ndvi": _number(row.get("ndvi_avg")),
            "ndmi": _number(row.get("ndmi_avg")),
            "official": True,
        }
        for row in rows
        if _number(row.get("ndvi_avg")) is not None
        and _number(row.get("ndmi_avg")) is not None
    ]
    drought_results = classify_drought_series(observations, season_months=season_months)
    if drought_results:
        # 只按最新可判定观测更新旱情，避免旧季节的干旱信号长期挂成当前预警。
        latest_drought_day, drought_class = drought_results[-1]
        drought_date = date.fromisoformat(latest_drought_day)
        if is_drought_day_class(drought_class):
            severity = {"mild": "low", "moderate": "medium", "severe": "high"}[drought_class]
            scene = next(row for row in reversed(rows) if row["date"] == drought_date)
            ndvi = _number(scene.get("ndvi_avg"))
            ndmi = _number(scene.get("ndmi_avg"))
            drought_label = {"mild": "轻度", "moderate": "中度", "severe": "重度"}[drought_class]
            created += _add_alert(
                session,
                land_id=land_id,
                alert_date=drought_date,
                rule_name="drought_risk",
                severity=severity,
                message=f"卫星指数显示{drought_label}干旱风险（NDVI {ndvi:.3f}，NDMI {ndmi:.3f}），建议核查土壤墒情和灌溉。",
                index_type="ndmi",
                params={"classification": drought_class, "ndvi": ndvi, "ndmi": ndmi},
                weather_context=_weather_context(session, land_id, drought_date),
                existing=existing,
                active=active,
            )
    evaluated.extend(
        {
            "index": key,
            "date": latest_date.isoformat(),
            "mean": round(value, 4),
        }
        for key in DEFAULT_INDEX_KEYS
        if (value := _number(latest.get(INDEX_COLUMNS[key]))) is not None
    )
    return created, evaluated


def _create_flood_alert(
    session,
    *,
    land_id: str,
    rows: list[dict[str, Any]],
    existing: dict[tuple[date, str], Alert | None],
    active: set[tuple[date, str]],
) -> tuple[int, list[dict[str, Any]]]:
    if not rows:
        return 0, []
    observations = [
        {
            "date": (_date(row.get("date")) or date.min).isoformat(),
            "scene_id": row.get("scene_id"),
            "relative_orbit": row.get("relative_orbit"),
            "vv": _number(row.get("vv_avg")),
            "vh": _number(row.get("vh_avg")),
        }
        for row in rows
    ]
    decisions = classify_flood_series(observations)
    if not decisions:
        return 0, []
    # 同日可能有不同轨道的 Sentinel-1 场景；按当日最严重信号生成一条预警。
    latest_date = max(_date(row.get("date")) or date.min for row in rows)
    latest_decisions = [
        (row, kind)
        for row, (day, kind) in zip(rows, decisions)
        if _date(day) == latest_date
    ]
    alert_date = latest_date
    severity_order = {"flood_severe": 3, "flood_moderate": 2, "watch": 1}
    candidates = [
        (row, kind)
        for row, kind in latest_decisions
        if kind in severity_order
    ]
    if not candidates:
        flood_class = next((kind for _, kind in reversed(latest_decisions)), None)
        return 0, [{"index": "flood", "date": alert_date.isoformat(), "class": flood_class}]
    scene, flood_class = max(candidates, key=lambda candidate: severity_order[candidate[1]])
    severity = {"watch": "low", "flood_moderate": "medium", "flood_severe": "high"}[flood_class]
    class_label = {"watch": "洪涝关注", "flood_moderate": "疑似中度洪涝", "flood_severe": "疑似重度洪涝"}[flood_class]
    spring_note = "春季信号也可能来自泡田或灌溉积水，请结合现场核查。" if alert_date.month in (3, 4, 5) else "建议尽快核查田间积水和排水情况。"
    created = _add_alert(
        session,
        land_id=land_id,
        alert_date=alert_date,
        rule_name="flood_risk",
        severity=severity,
        message=f"Sentinel-1 检测到{class_label}信号（VV {float(scene['vv_avg']):.1f} dB）；{spring_note}",
        index_type="flood",
        params={
            "classification": flood_class,
            "vv_db": _number(scene.get("vv_avg")),
            "vh_db": _number(scene.get("vh_avg")),
            "scene_id": scene.get("scene_id"),
        },
        weather_context=None,
        existing=existing,
        active=active,
    )
    return int(created), [{"index": "flood", "date": alert_date.isoformat(), "class": flood_class}]


def evaluate_agri_alerts_for_land(land_id: str, *, replace_open: bool = True) -> dict[str, Any]:
    """从 API 主库重算一个地块的最新长势、旱情和涝情预警。"""
    land_id = str(land_id).strip()
    if not land_id:
        return {"land_id": land_id, "status": "skipped", "reason": "empty_land_id"}

    session = SyncSession()
    try:
        land = session.get(LandParcel, land_id)
        if not land or land.deleted_at is not None:
            return {"land_id": land_id, "status": "skipped", "reason": "land_not_found"}

        as_of = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        optical = _load_optical_rows(session, land_id, as_of)
        sar = _load_sar_rows(session, land_id, as_of)
        rules = OPTICAL_RULES | FLOOD_RULES
        rows = (
            session.execute(
                select(Alert).where(Alert.land_id == land_id, Alert.rule_name.in_(rules))
            )
            .scalars()
            .all()
        )
        existing = {(row.date, row.rule_name): row for row in rows}
        active: set[tuple[date, str]] = set()
        created = 0
        evaluated: list[dict[str, Any]] = []
        latest_weather = _latest_weather_row(session, land_id, as_of)
        created += _create_weather_drought_alert(
            session,
            land_id=land_id,
            weather_row=latest_weather,
            existing=existing,
            active=active,
        )

        if optical:
            optical_created, optical_results = _create_optical_alerts(
                session,
                land_id=land_id,
                rows=optical,
                crop_type=land.crop_type,
                existing=existing,
                active=active,
            )
            created += optical_created
            evaluated.extend(optical_results)
        if sar:
            flood_created, flood_results = _create_flood_alert(
                session,
                land_id=land_id,
                rows=sar,
                existing=existing,
                active=active,
            )
            created += flood_created
            evaluated.extend(flood_results)

        removed = 0
        if replace_open:
            # 没有对应传感器数据时保留旧证据；有新观测时才清理不再命中的打开预警。
            for row in rows:
                key = (row.date, row.rule_name)
                if row.rule_name == "drought_risk":
                    has_sensor_data = bool(optical) or latest_weather is not None
                else:
                    has_sensor_data = bool(optical) if row.rule_name in OPTICAL_RULES else bool(sar)
                if row.status == "open" and has_sensor_data and key not in active:
                    session.delete(row)
                    removed += 1
        session.commit()
        return {
            "land_id": land_id,
            "status": "ok",
            "created": created,
            "removed_open": removed,
            "evaluated": evaluated,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def evaluate_alerts_for_scene_result(envelope: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any] | None:
    """场景或天气数据入库成功后统一重算，覆盖日常和手动回填链路。"""
    while isinstance(envelope.get("apply"), dict):
        envelope = envelope["apply"]
    while isinstance(stats.get("apply"), dict):
        stats = stats["apply"]
    scene_upserts = int(stats.get("scene_upserts") or 0)
    oss_stats = stats.get("oss")
    domain_stats = stats.get("domain") if isinstance(stats.get("domain"), dict) else {}
    weather_upserts = max(
        int(stats.get("weather_upserts") or 0),
        int(domain_stats.get("weather_rows") or 0),
    )
    if isinstance(oss_stats, dict):
        weather_upserts = max(weather_upserts, int(oss_stats.get("weather_upserts") or 0))
    scene_applied = scene_upserts > 0 or (
        isinstance(oss_stats, dict) and int(oss_stats.get("scene_upserts") or 0) > 0
    )
    if not scene_applied and weather_upserts <= 0:
        return None
    extras = envelope.get("extras") if isinstance(envelope.get("extras"), dict) else {}
    result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
    payload = envelope.get("payload") or envelope.get("inline")
    if not isinstance(payload, dict):
        payload = result.get("payload") or result.get("inline")
    if not isinstance(payload, dict):
        payload = result
    land_id = (
        extras.get("land_id")
        or result.get("land_id")
        or payload.get("land_id")
        or envelope.get("land_id")
    )
    sensor = str(extras.get("sensor") or result.get("sensor") or payload.get("sensor") or "").upper()
    if not land_id or (not scene_applied and weather_upserts <= 0):
        return None
    if scene_applied and sensor and sensor not in {"S1", "S2"} and weather_upserts <= 0:
        return None
    return evaluate_agri_alerts_for_land(str(land_id), replace_open=True)
