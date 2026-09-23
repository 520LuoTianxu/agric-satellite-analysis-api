"""按种植项目批量汇总地块监测、风险依据和待处理事项。"""

import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agri_classify import CLOUD_MAX_PCT, effective_cloud_pct, official_s2_sql
from app.models.tables import Alert, LandParcel
from app.schemas.project_monitoring import (
    ProjectAlert,
    ProjectLandMonitoring,
    ProjectMonitoringOut,
    ProjectObservation,
)

SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}
OPTICAL_INDICES = {"ndvi", "evi", "savi", "ndwi", "ndmi", "ndre", "cire", "mndwi"}


def finite_number(value: Any) -> float | None:
    """缺失和非有限值保持未知，不能用零替代监测指标或地块面积。"""
    if value is None or value == "":
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def observation_from_row(row: dict) -> ProjectObservation | None:
    if row.get("observation_date") is None:
        return None
    return ProjectObservation(
        date=row["observation_date"],
        ndvi=finite_number(row.get("ndvi_avg")),
        evi=finite_number(row.get("evi_avg")),
        ndmi=finite_number(row.get("ndmi_avg")),
        cloud_cover=finite_number(
            effective_cloud_pct(
                row.get("parcel_cloud_cover_pct"),
                row.get("cloud_cover"),
                parcel_cloud_source=row.get("parcel_cloud_source"),
                source=row.get("source"),
                scene_id=row.get("scene_id"),
            )
        ),
        source=row.get("source"),
    )


def summarize_land(
    land: dict,
    observations: list[ProjectObservation],
    alerts: list[Alert],
    *,
    as_of: date,
    freshness_days: int,
) -> ProjectLandMonitoring:
    """风险与处理状态独立：关闭最新观测的预警，不等于获得恢复证据。"""
    observation = observations[0] if observations else None
    if observation:
        data_status = (
            "fresh" if (as_of - observation.date).days <= freshness_days else "stale"
        )
    else:
        data_status = "low_quality" if land.get("latest_scene_date") else "missing"

    open_alerts = [alert for alert in alerts if alert.status == "open"]
    evidence = []
    for alert in alerts:
        # 未处理事项始终保留；关闭后需该指标的新观测，不能用只有 NDVI 的影像消除 NDMI 风险。
        optical = (alert.index_type or "").lower() in OPTICAL_INDICES and not (
            alert.rule_name.startswith("soil_")
        )
        latest_closed_evidence = (
            alert.status == "closed"
            and optical
            and (as_of - alert.date).days <= freshness_days
            and not any(
                point.date > alert.date
                and getattr(point, (alert.index_type or "").lower(), None) is not None
                for point in observations
            )
        )
        if alert.status == "open" or latest_closed_evidence:
            evidence.append(alert)

    evidence.sort(
        key=lambda alert: (
            SEVERITY_ORDER.get(alert.severity, 1),
            alert.date,
            str(alert.id),
        ),
        reverse=True,
    )
    risk_level = (
        evidence[0].severity
        if evidence
        else "normal"
        if data_status == "fresh"
        else "unknown"
    )
    if risk_level not in {"high", "medium", "low", "normal", "unknown"}:
        risk_level = "low"
    area_mu = finite_number(land.get("land_area_mu"))
    if area_mu is None:
        area_ha = finite_number(land.get("area_ha"))
        area_mu = area_ha * 15 if area_ha is not None else None
    return ProjectLandMonitoring(
        land_id=land["land_id"],
        land_name=land.get("land_name"),
        area_mu=area_mu if area_mu is not None and area_mu >= 0 else None,
        crop_type=land.get("crop_type"),
        boundary_geojson=land.get("boundary_geojson"),
        risk_level=risk_level,
        data_status=data_status,
        latest_scene_date=land.get("latest_scene_date"),
        observation=observation,
        previous_observation=observations[1] if len(observations) > 1 else None,
        open_alert_count=len(open_alerts),
        open_high_count=sum(alert.severity == "high" for alert in open_alerts),
        risk_alert_count=len(evidence),
        alerts=[
            ProjectAlert(
                id=str(alert.id),
                date=alert.date,
                severity=alert.severity,
                rule_name=alert.rule_name,
                message=alert.message,
                status=alert.status,
                index_type=alert.index_type,
            )
            for alert in evidence[:5]
        ],
    )


async def get_project_monitoring(
    db: AsyncSession, group_id: str, *, freshness_days: int = 14
) -> ProjectMonitoringOut:
    """仅在 API 机器批量读库；完整返回项目摘要，避免 500 条分页导致统计漏算。"""
    generated_at = datetime.now(timezone.utc)
    as_of = generated_at.astimezone(timezone(timedelta(hours=8))).date()
    # LATERAL 为每块地只取两个不同日期的有效产品，不向浏览器传输像元和多年历史。
    # 沿用官方光学质量规则；同日原始/去云产品只保留一份，避免把同日产品当作环比。
    rows = (
        (
            await db.execute(
                text(
                    f"""
                SELECT p.land_id, p.land_name, p.land_area_mu, p.area_ha,
                       p.crop_type, p.boundary_geojson,
                       latest.date AS latest_scene_date,
                       obs.date AS observation_date, obs.ndvi_avg, obs.evi_avg,
                       obs.ndmi_avg, obs.cloud_cover, obs.parcel_cloud_cover_pct,
                       obs.parcel_cloud_source, obs.product_source AS source,
                       obs.scene_id
                FROM agric_satellite.land_parcels p
                LEFT JOIN LATERAL (
                    SELECT s.date
                    FROM agric_satellite.parcel_scene_products s
                    WHERE s.land_id = p.land_id AND s.sensor = 'S2'
                      AND s.date <= :as_of
                    ORDER BY s.date DESC LIMIT 1
                ) latest ON true
                LEFT JOIN LATERAL (
                    SELECT DISTINCT ON (s.date)
                           s.date, s.ndvi_avg, s.evi_avg, s.ndmi_avg,
                           s.cloud_cover, s.parcel_cloud_cover_pct, s.scene_id,
                           s.parcel_cloud_source, s.product_source
                    FROM agric_satellite.parcel_scene_products s
                    WHERE s.land_id = p.land_id AND s.sensor = 'S2'
                      AND s.date <= :as_of AND s.ndvi_avg BETWEEN -1 AND 1
                      AND {official_s2_sql("s")}
                    ORDER BY s.date DESC,
                             CASE WHEN s.scene_id LIKE '%_decloud' THEN 1 ELSE 0 END,
                             s.scene_id
                    LIMIT 2
                ) obs ON true
                WHERE p.group_id = :group_id AND p.deleted_at IS NULL
                ORDER BY p.land_id, obs.date DESC
                """
                ),
                {"group_id": group_id, "as_of": as_of, "cloud_max": CLOUD_MAX_PCT},
            )
        )
        .mappings()
        .all()
    )

    alerts_by_land: dict[str, list[Alert]] = defaultdict(list)
    if rows:
        alerts = (
            (
                await db.execute(
                    select(Alert)
                    .join(LandParcel, LandParcel.land_id == Alert.land_id)
                    .where(
                        LandParcel.group_id == group_id,
                        LandParcel.deleted_at.is_(None),
                        Alert.date <= as_of,
                        or_(
                            Alert.status == "open",
                            Alert.date >= as_of - timedelta(days=freshness_days),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for alert in alerts:
            alerts_by_land[alert.land_id].append(alert)

    lands: dict[str, dict] = {}
    observations: dict[str, list[ProjectObservation]] = defaultdict(list)
    for row in rows:
        land_id = row["land_id"]
        lands.setdefault(land_id, dict(row))
        observation = observation_from_row(dict(row))
        if observation:
            observations[land_id].append(observation)

    return ProjectMonitoringOut(
        group_id=group_id,
        as_of=as_of,
        generated_at=generated_at,
        freshness_days=freshness_days,
        items=[
            summarize_land(
                land,
                observations[land_id],
                alerts_by_land[land_id],
                as_of=as_of,
                freshness_days=freshness_days,
            )
            for land_id, land in lands.items()
        ],
    )
