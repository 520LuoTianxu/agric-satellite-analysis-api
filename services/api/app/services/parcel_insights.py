"""基于历史观测生成可留存的营销分析，不混入当前告警或未来影像。"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from math import cos, radians, sqrt
from statistics import mean, pstdev
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text

from agric_satellite_analysis_common.harvest_detect import detect_harvest
from agric_satellite_analysis_common.phenology import infer_phenology, number
from app.core.agri_classify import is_decloud_product, is_official_optical_product
from app.models.tables import LandParcel, WeatherDaily
from app.schemas.parcel_insights import InsightsRequest

SPATIAL_DISPLAY_PIXEL_LIMIT = 1600


def shifted(day: date, year: int) -> date:
    """闰日映射至对照年的 2 月末，保留跨年区间的年偏移。"""
    try:
        return day.replace(year=year)
    except ValueError:
        return day.replace(year=year, day=28)


def official_points(rows: list[dict]) -> list[dict]:
    """同日只留一个有效产品，优先清晰原始影像；不让重建重复增加证据数量。"""
    # SQL按规范列优先并回退旧JSONB，确保不同入库路径使用同一去云/云量来源判据。
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        value = number(row.get("ndvi_avg"))
        if value is None or not -1 <= value <= 1:
            continue
        if not is_official_optical_product(
            **{
                key: row.get(key)
                for key in (
                    "source",
                    "scene_id",
                    "decloud_quality",
                    "parcel_cloud_cover_pct",
                    "cloud_cover",
                    "cloud_cover_over_30",
                    "parcel_cloud_source",
                )
            }
        ):
            continue
        grouped[str(row["date"])[:10]].append(row)
    points = []
    for day, candidates in sorted(grouped.items()):
        row = min(
            candidates,
            key=lambda r: (
                is_decloud_product(r.get("source"), r.get("scene_id")),
                number(r.get("parcel_cloud_cover_pct"))
                if number(r.get("parcel_cloud_cover_pct")) is not None
                else 101,
                str(r.get("scene_id") or ""),
            ),
        )
        points.append(
            {
                "date": day,
                "ndvi": number(row["ndvi_avg"]),
                "ndvi_avg": number(row["ndvi_avg"]),
                "evi": number(row.get("evi_avg")),
                "ndmi": number(row.get("ndmi_avg")),
                "scene_id": row.get("scene_id"),
                "source": row.get("source"),
                "decloud_quality": row.get("decloud_quality"),
                "official": True,
            }
        )
    return points


def summary(points: list[dict]) -> dict:
    """汇总有效观测数量、NDVI/NDMI统计和最大日期间隔，不对缺测日插值。"""
    values = [p["ndvi"] for p in points]
    moisture = [p["ndmi"] for p in points if p.get("ndmi") is not None]
    return {
        "count": len(points),
        "first_date": points[0]["date"] if points else None,
        "last_date": points[-1]["date"] if points else None,
        "mean_ndvi": round(mean(values), 4) if values else None,
        "peak_ndvi": max(values) if values else None,
        "mean_ndmi": round(mean(moisture), 4) if moisture else None,
        "change": round(values[-1] - values[0], 4) if len(values) >= 2 else None,
        "max_gap_days": max(
            (
                (date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days
                for a, b in zip(points, points[1:])
            ),
            default=0,
        ),
    }


def matched_comparison(series: dict[str, list[dict]], *, shift_years: int = 0) -> dict:
    """按日期近邻一对一匹配，最多相差 3 天；禁止用不同日期的最新值硬排名。"""
    keys = list(series)
    result = {
        "status": "insufficient_data",
        "count": 0,
        "date_tolerance_days": 3,
        "means": {},
        "differences": {},
    }
    if len(keys) < 2:
        return result
    used = {key: set() for key in keys}
    values: dict[str, list[float]] = {key: [] for key in keys}
    for anchor in series[keys[0]]:
        day = date.fromisoformat(anchor["date"])
        matches = {keys[0]: anchor}
        for key in keys[1:]:

            def distance(point):
                other = date.fromisoformat(point["date"])
                if shift_years:
                    other = shifted(other, other.year + shift_years)
                return abs((day - other).days)

            candidates = [
                p
                for p in series[key]
                if p["date"] not in used[key] and distance(p) <= 3
            ]
            if not candidates:
                break
            matches[key] = min(candidates, key=lambda p: (distance(p), p["date"]))
        if len(matches) == len(keys):
            # 多块地的最早和最晚日期也须相差不超过 3 天，不能各偏离基准 3 天形成 6 天跨度。
            matched_days = [
                date.fromisoformat(point["date"]) for point in matches.values()
            ]
            matched_days = [
                day
                if index == 0 or not shift_years
                else shifted(day, day.year + shift_years)
                for index, day in enumerate(matched_days)
            ]
            if (max(matched_days) - min(matched_days)).days > 3:
                continue
            for key, point in matches.items():
                used[key].add(point["date"])
                values[key].append(point["ndvi"])
    result["count"] = len(values[keys[0]])
    if result["count"] >= 3:
        result["status"] = "matched"
        result["means"] = {key: round(mean(v), 4) for key, v in values.items()}
        result["differences"] = {
            key: round(mean(v) - mean(values[keys[0]]), 4) for key, v in values.items()
        }
    return result


def event_review(event: dict, series: dict[str, list[dict]]) -> dict:
    """措施前后使用同样长度的窗口；有对照也只报告变化，不声称措施因果效果。"""
    day = date.fromisoformat(str(event["date"]))
    span = timedelta(days=event["window_days"])

    def split(points):
        before = [
            p for p in points if day - span <= date.fromisoformat(p["date"]) < day
        ]
        after = [p for p in points if day < date.fromisoformat(p["date"]) <= day + span]
        delta = (
            round(mean(p["ndvi"] for p in after) - mean(p["ndvi"] for p in before), 4)
            if len(before) >= 2 and len(after) >= 2
            else None
        )
        return {"before": summary(before), "after": summary(after), "change": delta}

    result = {
        **event,
        **split(series[event["land_id"]]),
        "control": None,
        "relative_change": None,
        "note_on_method": "用户录入的措施记录；前后各至少 2 个有效观测日。自然生长和天气也影响绿度，变化不等于增产或措施的因果效果。",
    }
    control = event.get("control_land_id")
    if control:
        # 对照差值必须来自同步观测，以免不同采样日期伪造服务效果。
        target_points, control_points = series[event["land_id"]], series[control]
        before = matched_comparison(
            {
                "target": [
                    p
                    for p in target_points
                    if day - span <= date.fromisoformat(p["date"]) < day
                ],
                "control": [
                    p
                    for p in control_points
                    if day - span <= date.fromisoformat(p["date"]) < day
                ],
            }
        )
        after = matched_comparison(
            {
                "target": [
                    p
                    for p in target_points
                    if day < date.fromisoformat(p["date"]) <= day + span
                ],
                "control": [
                    p
                    for p in control_points
                    if day < date.fromisoformat(p["date"]) <= day + span
                ],
            }
        )
        result["control"] = {
            "land_id": control,
            **split(control_points),
            "matched_before": before["count"],
            "matched_after": after["count"],
        }
        if before["status"] == after["status"] == "matched":
            result["relative_change"] = round(
                (after["means"]["target"] - before["means"]["target"])
                - (after["means"]["control"] - before["means"]["control"]),
                4,
            )
    return result


async def load_points(
    db,
    land_ids: list[str],
    start: date,
    end: date,
    reference: tuple[date, date] | None = None,
) -> dict[str, list[dict]]:
    """一次读取目标期和可选参照期的 S2 候选景，再按统一质量口径去重。"""
    # 将所选与参照日期并入同一条地块集合查询，并只取洞察所需字段，避免逐地块往返或扫描整段无关历史。
    params = {
        "land_ids": land_ids,
        "start": start,
        "end": end,
        "ref_start": reference[0] if reference else start,
        "ref_end": reference[1] if reference else end,
    }
    rows = (
        (
            await db.execute(
                text("""
        SELECT land_id, date, scene_id, ndvi_avg, evi_avg, ndmi_avg,
               cloud_cover, parcel_cloud_cover_pct,
               COALESCE(NULLIF(BTRIM(product_source), ''), NULLIF(BTRIM(pixel_data->>'source'), '')) AS source,
               COALESCE(NULLIF(BTRIM(decloud_quality), ''), NULLIF(BTRIM(pixel_data->>'decloud_quality'), '')) AS decloud_quality,
               COALESCE(NULLIF(BTRIM(parcel_cloud_source), ''), NULLIF(BTRIM(pixel_data->>'parcel_cloud_source'), '')) AS parcel_cloud_source,
               cloud_cover_over_30
        FROM agric_satellite.parcel_scene_products
        WHERE land_id = ANY(:land_ids) AND sensor = 'S2'
          AND ((date BETWEEN :start AND :end) OR (date BETWEEN :ref_start AND :ref_end))
        ORDER BY land_id, date, scene_id
    """),
                params,
            )
        )
        .mappings()
        .all()
    )
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["land_id"]].append(dict(row))
    return {key: official_points(grouped[key]) for key in land_ids}


def _sample_spatial_pixels(valid: list[list[float]]) -> list[list[float]]:
    """按地理网格选展示代表点，避免展平数组固定步长抽样产生周期性空间偏差。"""
    if len(valid) <= SPATIAL_DISPLAY_PIXEL_LIMIT:
        return valid

    min_lon = min(pixel[0] for pixel in valid)
    max_lon = max(pixel[0] for pixel in valid)
    min_lat = min(pixel[1] for pixel in valid)
    max_lat = max(pixel[1] for pixel in valid)
    # 经度距离随纬度收缩；用近似投影后的宽高比设置网格，避免狭长地块浪费抽样格。
    longitude_factor = max(abs(cos(radians((min_lat + max_lat) / 2))), 1e-12)
    width = (max_lon - min_lon) * longitude_factor
    height = max_lat - min_lat
    if width and height:
        aspect_ratio = width / height
    elif width:
        aspect_ratio = float(SPATIAL_DISPLAY_PIXEL_LIMIT)
    elif height:
        aspect_ratio = 1 / SPATIAL_DISPLAY_PIXEL_LIMIT
    else:
        aspect_ratio = 1.0

    columns = max(
        1,
        min(
            SPATIAL_DISPLAY_PIXEL_LIMIT,
            round(sqrt(SPATIAL_DISPLAY_PIXEL_LIMIT * aspect_ratio)),
        ),
    )
    rows = max(1, SPATIAL_DISPLAY_PIXEL_LIMIT // columns)
    cell_width = width / columns
    cell_height = height / rows
    representatives: dict[tuple[int, int], tuple[float, list[float]]] = {}
    for pixel in valid:
        x = (pixel[0] - min_lon) * longitude_factor
        y = pixel[1] - min_lat
        column = min(columns - 1, int(x / width * columns)) if width else 0
        row = min(rows - 1, int(y / height * rows)) if height else 0
        center_x = (column + 0.5) * cell_width
        center_y = (row + 0.5) * cell_height
        # 每格取最靠近格心的有效像元；距离相同时按坐标和NDVI稳定择一，不依赖原数组的行扫描顺序。
        distance = (((x - center_x) / cell_width) ** 2 if cell_width else 0) + (
            ((y - center_y) / cell_height) ** 2 if cell_height else 0
        )
        key = (row, column)
        previous = representatives.get(key)
        if previous is None or (distance, *pixel) < (previous[0], *previous[1]):
            representatives[key] = (distance, pixel)

    return [representatives[key][1] for key in sorted(representatives)]


def _spatial_snapshot_from_data(point: dict, pixel_data: Any) -> dict | None:
    """仅用明确标清且坐标/NDVI有效的像元计算全量统计，再生成地图代表点。"""
    if not isinstance(pixel_data, dict) or pixel_data.get("format") != "lonlat_v1":
        return None
    pixels = pixel_data.get("pixels") or []
    valid = []
    for pixel in pixels:
        # 像元缺质量标记时不假定清晰；地块日均有效不代表每一个像元都可用。
        if not isinstance(pixel, dict) or pixel.get("clear") not in (1, True):
            continue
        lon, lat, value = (
            number(pixel.get("lon")),
            number(pixel.get("lat")),
            number(pixel.get("NDVI")),
        )
        if (
            lon is not None
            and lat is not None
            and value is not None
            and -180 <= lon <= 180
            and -90 <= lat <= 90
            and -1 <= value <= 1
        ):
            valid.append([lon, lat, value])
    if not valid:
        return None
    display_pixels = _sample_spatial_pixels(valid)
    return {
        "date": point["date"],
        "valid_pixels": len(valid),
        "total_pixels": len(pixels),
        "low_green_pct": round(sum(p[2] < 0.35 for p in valid) / len(valid) * 100, 1),
        "ndvi_stddev": round(pstdev(p[2] for p in valid), 4),
        "pixels": display_pixels,
        "display_sampled": len(display_pixels) < len(valid),
        "note": "低绿度比例仅按有效像元统计（NDVI < 0.35），不代表受灾面积。",
    }


async def spatial_snapshots(
    db, points_by_land: dict[str, dict | None]
) -> dict[str, dict | None]:
    """一次批量读取各地块最新展示景的像元，并按地块生成空间快照。"""
    # 多地块洞察一次批量取各自最新展示景，避免在地块循环内串行产生最多20次数据库往返。
    snapshots = {land_id: None for land_id in points_by_land}
    requested = [
        (land_id, point)
        for land_id, point in points_by_land.items()
        if point is not None
    ]
    if not requested:
        return snapshots

    values = ", ".join(
        "(CAST(:land_{i} AS text), CAST(:day_{i} AS date), CAST(:scene_{i} AS text))".format(
            i=index
        )
        for index in range(len(requested))
    )
    params = {}
    for index, (land_id, point) in enumerate(requested):
        params.update(
            {
                f"land_{index}": land_id,
                f"day_{index}": date.fromisoformat(point["date"]),
                f"scene_{index}": point["scene_id"],
            }
        )
    rows = (
        (
            await db.execute(
                text(f"""
        WITH requested(land_id, scene_date, scene_id) AS (VALUES {values})
        SELECT requested.land_id, product.pixel_data
        FROM requested
        JOIN LATERAL (
            SELECT pixel_data
            FROM agric_satellite.parcel_scene_products
            WHERE land_id = requested.land_id
              AND sensor = 'S2'
              AND date = requested.scene_date
              AND scene_id = requested.scene_id
            LIMIT 1
        ) AS product ON TRUE
    """),
                params,
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        land_id = row["land_id"]
        point = points_by_land[land_id]
        if point is not None:
            snapshots[land_id] = _spatial_snapshot_from_data(point, row["pixel_data"])
    return snapshots


async def build_insights(db, request: InsightsRequest) -> dict[str, Any]:
    """组合有效 S2 时序、降雨、物候、收获与空间证据，并保留缺测和不确定状态。"""
    lands = (
        (
            await db.execute(
                select(LandParcel).where(
                    LandParcel.land_id.in_(request.land_ids),
                    LandParcel.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {land.land_id: land for land in lands}
    if len(by_id) != len(request.land_ids):
        raise HTTPException(404, "部分地块不存在或已删除，请重新选择")
    reference = None
    if request.reference_year is not None:
        reference = (
            shifted(request.start_date, request.reference_year),
            shifted(
                request.end_date,
                request.reference_year
                + request.end_date.year
                - request.start_date.year,
            ),
        )
    all_points = await load_points(
        db, request.land_ids, request.start_date, request.end_date, reference
    )
    series = {
        key: [
            p
            for p in points
            if request.start_date.isoformat()
            <= p["date"]
            <= request.end_date.isoformat()
        ]
        for key, points in all_points.items()
    }
    # 人工确认的一茬决定该地块统计范围，不能只改变标签而仍使用全年平均。
    for land_id, window in request.seasons.items():
        series[land_id] = [
            p
            for p in series[land_id]
            if window.start_date.isoformat() <= p["date"] <= window.end_date.isoformat()
        ]
    weather = (
        await db.execute(
            select(
                WeatherDaily.land_id, WeatherDaily.date, WeatherDaily.precipitation_sum
            ).where(
                WeatherDaily.land_id.in_(request.land_ids),
                WeatherDaily.date.between(request.start_date, request.end_date),
            )
        )
    ).all()
    rain: dict[str, list[float]] = defaultdict(list)
    for land_id, day, value in weather:
        # 人工窗口筛选后的降雨和遥感指标保持同一日期范围，缺测仍不能补成零。
        window = request.seasons.get(land_id)
        if window and not window.start_date <= day <= window.end_date:
            continue
        parsed = number(value)
        if parsed is not None:
            rain[land_id].append(parsed)
    spatial_by_land = await spatial_snapshots(
        db,
        {land_id: points[-1] if points else None for land_id, points in series.items()},
    )
    items = []
    for land_id in request.land_ids:
        land, points = by_id[land_id], series[land_id]
        phenology = infer_phenology(
            points, start=request.start_date, end=request.end_date
        )
        override = request.seasons.get(land_id)
        effective = (
            [
                {
                    "start_date": override.start_date.isoformat(),
                    "end_date": override.end_date.isoformat(),
                    "status": "manual",
                    "confidence": "user",
                    "peak_date": None,
                }
            ]
            if override
            else phenology["windows"]
        )
        harvests = []
        for window_index, window in enumerate(effective):
            start = window.get("start_date") or window.get("observed_start")
            # 窗口末尾需纳入已存在的低值观测验证收获表现，但绝不越过报告截止日。
            if override:
                end = window["end_date"]
            else:
                # 收后至少再观察一期以验证低值持续；下一茬开始后不再归入上一茬。
                next_window = (
                    effective[window_index + 1]
                    if window_index + 1 < len(effective)
                    else None
                )
                next_start = (
                    (next_window.get("start_date") or next_window.get("observed_start"))
                    if next_window
                    else None
                )
                end = (
                    min(
                        request.end_date,
                        date.fromisoformat(next_start) - timedelta(days=1),
                    ).isoformat()
                    if next_start
                    else request.end_date.isoformat()
                )
            crop_points = [p for p in points if start <= p["date"] <= end]
            detection = detect_harvest(
                crop_points, window={"start_date": start, "end_date": end}
            ).to_dict()
            harvests.append(detection)
        observed = summary(points)
        last_gap = (
            (request.end_date - date.fromisoformat(points[-1]["date"])).days
            if points
            else None
        )
        confirmed = [
            h
            for h in harvests
            if h["status"] == "detected" and h["confidence"] in ("medium", "high")
        ]
        latest_window = effective[-1] if effective else None
        # 较早一茬的收获不能覆盖下一茬已恢复生长的状态。
        latest_harvest = harvests[-1] if harvests else None
        if not points or len(points) < 6 or last_gap > 35:
            progress = "unknown"
        elif latest_harvest and latest_harvest in confirmed:
            progress = "harvest_signal"
        elif (
            latest_window
            and (latest_window.get("end_date") or latest_window.get("observed_end", ""))
            >= points[-1]["date"]
        ):
            progress = "growth_signal"
        else:
            progress = "needs_check"
        baseline_start, baseline_end = reference if reference else (None, None)
        if reference and override:
            shift = request.start_date.year - reference[0].year
            baseline_start = shifted(
                override.start_date, override.start_date.year - shift
            )
            baseline_end = shifted(override.end_date, override.end_date.year - shift)
        baseline_points = [
            p
            for p in all_points[land_id]
            if reference
            and baseline_start.isoformat() <= p["date"] <= baseline_end.isoformat()
        ]
        history = None
        if reference:
            history = {
                "start_date": baseline_start.isoformat(),
                "end_date": baseline_end.isoformat(),
                "series": baseline_points,
                "summary": summary(baseline_points),
                "comparison": matched_comparison(
                    {"selected": points, "reference": baseline_points},
                    shift_years=request.start_date.year - reference[0].year,
                ),
                "note": "按相近日历日期匹配；历史作物、品种和播期尚未核实，不能直接作为同生育阶段或产量对比。",
            }
        notes = []
        if not points:
            notes.append("区间内没有有效光学观测，不能判断地块长势。")
        elif observed["max_gap_days"] > 35 or last_gap > 35:
            notes.append("观测存在较长空缺，变化时间与期末状态需核查。")
        if phenology["status"] != "detected" and not override:
            notes.append("生育窗未能自动确定，可填写人工窗口再分析。")
        if land.crop_type is None:
            notes.append("未登记作物；请补充作物信息后解释地块差异。")
        spatial = spatial_by_land[land_id]
        if spatial and spatial["valid_pixels"] < 9:
            notes.append("可用像元很少，边界混合对结果影响较大；空间比例只作线索。")
        items.append(
            {
                "land_id": land_id,
                "land_name": land.land_name or land_id,
                "crop": override.crop if override and override.crop else land.crop_type,
                "group_name": land.group_name,
                "area_mu": number(land.land_area_mu)
                if land.land_area_mu is not None
                else (
                    number(land.area_ha) * 15
                    if number(land.area_ha) is not None
                    else None
                ),
                "boundary": land.boundary_geojson,
                "series": points,
                "summary": observed,
                "phenology": phenology,
                "effective_windows": effective,
                "season_source": "manual" if override else "observed",
                "harvests": harvests,
                "progress": progress,
                "history": history,
                "spatial": spatial,
                "notes": notes,
                "rainfall": {
                    "mm": round(sum(rain[land_id]), 1) if rain[land_id] else None,
                    "observed_days": len(rain[land_id]),
                    "period_days": (
                        (override.end_date - override.start_date).days
                        if override
                        else (request.end_date - request.start_date).days
                    )
                    + 1,
                },
            }
        )
    comparison = matched_comparison(series)
    crops = {item["crop"] for item in items}
    comparison["comparable_crop"] = (
        len(crops) == 1 and None not in crops and "" not in crops
    )
    comparison["note"] = (
        "只比较匹配日期的冠层绿度；作物、播期、品种和管理差异仍需核实，不做产量或地力排名。"
    )
    return {
        "version": 1,
        "request": request.model_dump(mode="json"),
        "items": items,
        "comparison": comparison,
        "progress_counts": dict(Counter(item["progress"] for item in items)),
        "events": [
            event_review(event.model_dump(mode="json"), series)
            for event in request.events
        ],
        "data_note": "本分析仅使用所选区间内已入库的有效 Sentinel-2 观测。空间图分别标注观测日；当前作物登记不等于历史种植记录。",
    }
