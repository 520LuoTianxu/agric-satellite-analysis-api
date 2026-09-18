"""只聚合请求中的地块，使用当地米制投影计算5×5公里区域。"""

import math
from collections.abc import Sequence

from pyproj import CRS, Transformer
from shapely.geometry import box
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

from app.core.geo import geojson_to_shape
from app.models.tables import LandParcel
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
