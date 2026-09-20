"""只聚合请求中的地块，使用当地米制投影计算5×5公里区域。"""

import uuid
import math
from collections.abc import Sequence
from datetime import date, timedelta

from pyproj import CRS, Transformer
from shapely.geometry import box
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

from app.core.config import settings
from app.core.geo import geojson_to_shape
from app.models.tables import Job, LandParcel
from app.schemas.satellite_batch import SatelliteBatchGroup


def satellite_land_geometry(land: LandParcel):
    """统一校验下载所需边界，定时任务可单独隔离无效地块而不中断全国批次。"""
    geom = geojson_to_shape(land.boundary_geojson)
    if (
        land.boundary_srid != 4326
        or geom is None
        or geom.is_empty
        or not geom.is_valid
        or geom.geom_type not in {"Polygon", "MultiPolygon"}
        or not all(math.isfinite(value) for value in geom.bounds)
        or geom.bounds[0] < -180
        or geom.bounds[2] > 180
        or geom.bounds[1] <= -90
        or geom.bounds[3] >= 90
        or geom.bounds[2] - geom.bounds[0] >= 180
    ):
        raise ValueError(f"地块{land.land_id}缺少有效的WGS84多边形边界")
    return geom


def group_satellite_lands(lands: Sequence[LandParcel]) -> list[SatelliteBatchGroup]:
    """按稳定地块顺序选锚点；完整包含才共享窗口，超大地块独立处理。"""
    geometries = {land.land_id: satellite_land_geometry(land) for land in lands}

    remaining = dict(sorted(geometries.items()))
    land_keys = list(remaining)
    # 全国清单先用空间索引筛邻居，避免每个锚点遍历所有远处地块。
    tree = STRtree(list(remaining.values()))
    groups = []
    while remaining:
        anchor_id = next(iter(remaining))
        anchor = remaining[anchor_id]
        center = anchor.centroid
        # 经纬度单位不是公里；以锚点建立局部等距投影，避免高纬地区尺寸失真。
        local_crs = CRS.from_proj4(
            f"+proj=aeqd +lat_0={center.y} +lon_0={center.x} +datum=WGS84 +units=m"
        )
        to_local = Transformer.from_crs(4326, local_crs, always_xy=True)
        to_wgs = Transformer.from_crs(local_crs, 4326, always_xy=True)
        square = box(-2500, -2500, 2500, 2500)
        aggregation_bbox = transform(to_wgs.transform, square.segmentize(100)).bounds
        oversized = not square.covers(transform(to_local.transform, anchor))
        members = [anchor_id]
        if not oversized:
            candidates = sorted(
                land_keys[int(i)] for i in tree.query(box(*aggregation_bbox))
            )
            members.extend(
                land_id
                for land_id in candidates
                # 先用经纬度外接范围排除远处地块，避免离散清单做大量投影转换。
                if land_id != anchor_id
                and land_id in remaining
                and (geom := remaining[land_id]) is not None
                and geom.bounds[0] >= aggregation_bbox[0]
                and geom.bounds[1] >= aggregation_bbox[1]
                and geom.bounds[2] <= aggregation_bbox[2]
                and geom.bounds[3] <= aggregation_bbox[3]
                and square.covers(transform(to_local.transform, geom))
            )
        # 保留组内地块外接范围供超大/旧任务兼容；普通下载任务使用 aggregation_bbox。
        download_bbox = unary_union([remaining.pop(key) for key in members]).bounds
        groups.append(
            SatelliteBatchGroup(
                anchor_land_id=anchor_id,
                land_ids=members,
                aggregation_bbox=aggregation_bbox,
                download_bbox=download_bbox,
                oversized=oversized,
            )
        )
    return groups


def build_satellite_batch_jobs(
    lands: Sequence[LandParcel],
    *,
    date_from: date,
    date_to: date,
    sensors: Sequence[str],
    force: bool = False,
    parent_job_id: uuid.UUID | None = None,
    id_namespace: uuid.UUID | None = None,
    chunk_days: int | None = None,
) -> tuple[list[SatelliteBatchGroup], list[Job]]:
    """为一批地块构造共享遥感 Job，不在这里提交数据库或派发消息。

    批量选地报告和独立遥感回填都需要完全一致的 5×5 km 分组、日期分片
    和任务参数；集中构造可以避免两个入口逐渐产生不同的聚合规则。
    ``id_namespace`` 用于批量报告重试时生成稳定 Job ID，防止重复下载。
    """
    if date_from > date_to:
        raise ValueError("date_from must be no later than date_to")

    groups = group_satellite_lands(lands)
    jobs: list[Job] = []
    unique_sensors = list(dict.fromkeys(str(sensor) for sensor in sensors))
    chunk_days = max(
        int(settings.index_backfill_chunk_days if chunk_days is None else chunk_days), 1
    )
    processing_window_km = 5.0

    for group in groups:
        cursor = date_from
        while cursor <= date_to:
            end = min(cursor + timedelta(days=chunk_days - 1), date_to)
            for sensor in unique_sensors:
                if id_namespace is None:
                    job_id = uuid.uuid4()
                else:
                    job_id = uuid.uuid5(
                        id_namespace,
                        f"satellite_batch:{group.anchor_land_id}:{sensor}:"
                        f"{cursor.isoformat()}:{end.isoformat()}",
                    )
                job = Job(
                    id=job_id,
                    land_id=group.anchor_land_id,
                    type="satellite_batch",
                    status="pending",
                    parent_job_id=parent_job_id,
                    params_json={
                        "land_ids": group.land_ids,
                        "anchor_land_id": group.anchor_land_id,
                        "processing_window_km": processing_window_km,
                        "oversized": group.oversized,
                        "download_bbox": list(group.download_bbox),
                        "aggregation_bbox": list(group.aggregation_bbox),
                        "sensor": sensor,
                        "date_from": cursor.isoformat(),
                        "date_to": end.isoformat(),
                        "force": force,
                    },
                )
                group.job_ids.append(str(job.id))
                jobs.append(job)
            cursor = end + timedelta(days=1)
    return groups, jobs
