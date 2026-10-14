"""项目级（group）全波段 Sentinel-2 L2A COG 的纯函数部分：分块、键、编码、掩膜、写 COG。

产品口径（v1）：
- 每个项目按地块空间聚类成若干计算块；每块每个采集日一个 COG；
- 12 个 L2A 波段（B01..B12，含 B8A；20/60 m 双线性重采样到 10 m）+ SCL（最近邻），共 13 波段；
- uint16，反射率×10000（SCL 为原分类值），nodata=0；地块外扩 20 m 以外置 0；
- ZSTD + predictor 2，256×256 瓦片，内建概视图（nearest）。
"""

from __future__ import annotations

import json

import math
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

PRODUCT = "s2l2a_v1"
COG_FORMAT_VERSION = 1
RESOLUTION_M = 10.0
BLOCK_SNAP_M = 1280.0  # 128 像元；块原点对齐到该网格，保证同一地块集合得到同一块键
# (波段名, Earth Search sentinel-2-l2a 资产名)
S2_L2A_BANDS: tuple[tuple[str, str], ...] = (
    ("B01", "coastal"),
    ("B02", "blue"),
    ("B03", "green"),
    ("B04", "red"),
    ("B05", "rededge1"),
    ("B06", "rededge2"),
    ("B07", "rededge3"),
    ("B08", "nir"),
    ("B8A", "nir08"),
    ("B09", "nir09"),
    ("B11", "swir16"),
    ("B12", "swir22"),
)
SCL_BAND = ("SCL", "scl")
BAND_NAMES: tuple[str, ...] = tuple(b for b, _ in S2_L2A_BANDS) + (SCL_BAND[0],)


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def project_cogs_enabled() -> bool:
    """自动生成开关（卫星批任务结束后触发），默认关闭；手动补数不受此开关限制。"""
    return _truthy(os.environ.get("PROJECT_SCENE_COGS_ENABLED"))


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, value)


def max_block_pixels() -> int:
    """单块网格像元上限（内存保护）；默认 400 万（与 ADR-0030 网格上限一致）。"""
    return int(_env_float("PROJECT_SCENE_COGS_MAX_BLOCK_PIXELS", 4_000_000, 65_536))


def mask_buffer_m() -> float:
    return _env_float("PROJECT_SCENE_COGS_MASK_BUFFER_M", 20.0, 0.0)


def cluster_link_m() -> float:
    """地块包络相距不超过该距离即并入同一块。"""
    return _env_float("PROJECT_SCENE_COGS_LINK_M", 2000.0, 0.0)


def key_prefix() -> str:
    return str(os.environ.get("PROJECT_SCENE_COGS_PREFIX") or "cog/s2l2a/v1").strip("/")


def cog_key(group_id: str, block_key: str, obs_date: str, prefix: str | None = None) -> str:
    """确定性 OSS 键：<prefix>/group_<id>/<block>/<YYYY-MM-DD>.tif。"""
    gid = str(group_id).strip()
    if not gid or "/" in gid:
        raise ValueError(f"invalid group_id {group_id!r}")
    return f"{prefix or key_prefix()}/group_{gid}/{block_key}/{str(obs_date)[:10]}.tif"


def sidecar_key(tif_key: str) -> str:
    return tif_key[: -len(".tif")] + ".json" if tif_key.endswith(".tif") else tif_key + ".json"


@dataclass
class Block:
    key: str
    crs: str
    x0: float  # 左边界（米，已对齐 10 m）
    y1: float  # 上边界
    width: int
    height: int
    land_ids: list[str] = field(default_factory=list)
    geoms: list[Any] = field(default_factory=list)  # 投影坐标系下的地块几何

    @property
    def transform(self):
        from rasterio.transform import from_origin

        return from_origin(self.x0, self.y1, RESOLUTION_M, RESOLUTION_M)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            self.x0,
            self.y1 - self.height * RESOLUTION_M,
            self.x0 + self.width * RESOLUTION_M,
            self.y1,
        )

    def bounds_4326(self) -> tuple[float, float, float, float]:
        from rasterio.warp import transform_bounds

        return tuple(transform_bounds(self.crs, "EPSG:4326", *self.bounds, densify_pts=21))


def parcel_geometry(geojson: Mapping[str, Any] | str | None):
    """解析并修复地块边界（自相交 make_valid），只保留面要素；无效返回 None。"""
    from shapely.geometry import MultiPolygon, shape
    from shapely.validation import make_valid

    try:
        if isinstance(geojson, str):
            geojson = json.loads(geojson)
        if isinstance(geojson, Mapping) and geojson.get("type") == "Feature":
            geojson = geojson.get("geometry")
        geom = shape(geojson)
    except Exception:
        return None
    if not geom.is_valid:
        geom = make_valid(geom)
    polys: list[Any] = []
    for part in getattr(geom, "geoms", [geom]):
        if part.geom_type == "Polygon" and not part.is_empty:
            polys.append(part)
        elif part.geom_type == "MultiPolygon":
            polys.extend(p for p in part.geoms if not p.is_empty)
    if not polys:
        return None
    out = MultiPolygon(polys) if len(polys) > 1 else polys[0]
    x0, y0, x1, y1 = out.bounds
    if max(x1 - x0, y1 - y0) > max_span_deg():
        return None  # 明显错误的边界（如跨 0.25°），不参与分块/掩膜
    return out


def max_span_deg() -> float:
    try:
        return float(os.environ.get("PROJECT_SCENE_COGS_MAX_SPAN_DEG", "0.05"))
    except ValueError:
        return 0.05


def _snap_down(v: float, step: float) -> float:
    return math.floor(v / step) * step


def _snap_up(v: float, step: float) -> float:
    return math.ceil(v / step) * step


def build_blocks(
    parcels: Sequence[Mapping[str, Any]],
    *,
    crs: str | None = None,
    buffer_m: float | None = None,
    link_m: float | None = None,
    max_pixels: int | None = None,
) -> list[Block]:
    """把项目地块聚成计算块；超过像元上限的簇再按方格切分。

    ``parcels`` 元素需含 ``land_id`` 与 ``boundary_geojson``（WGS84）。
    """
    from pyproj import Transformer
    from shapely.geometry import box
    from shapely.ops import transform as shp_transform
    from shapely.ops import unary_union

    buffer_m = mask_buffer_m() if buffer_m is None else buffer_m
    link_m = cluster_link_m() if link_m is None else link_m
    max_pixels = max_block_pixels() if max_pixels is None else max_pixels

    items: list[tuple[str, Any]] = []
    for p in parcels:
        g = parcel_geometry(p.get("boundary_geojson") or {})
        if g is not None:
            items.append((str(p["land_id"]), g))
    if not items:
        return []
    if crs is None:
        from app.tasks.pipeline import analysis_crs_for_bounds

        crs = analysis_crs_for_bounds(unary_union([g for _, g in items]).bounds)
    to = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    proj = [(lid, shp_transform(to, g)) for lid, g in items]

    # 单连接聚类：包络各外扩 link/2，相交即同簇
    half = link_m / 2.0 + buffer_m
    hulls = unary_union([box(*g.bounds).buffer(half, join_style=2) for _, g in proj])
    comps = list(getattr(hulls, "geoms", [hulls]))
    clusters: list[list[tuple[str, Any]]] = [[] for _ in comps]
    for lid, g in proj:
        c = g.centroid
        idx = next((i for i, h in enumerate(comps) if h.contains(c)), None)
        if idx is None:
            idx = min(range(len(comps)), key=lambda i: comps[i].distance(c))
        clusters[idx].append((lid, g))

    side = max(BLOCK_SNAP_M, _snap_down(math.sqrt(max_pixels) * RESOLUTION_M, BLOCK_SNAP_M))
    blocks: dict[str, Block] = {}
    for members in clusters:
        if not members:
            continue
        minx = min(g.bounds[0] for _, g in members) - buffer_m
        miny = min(g.bounds[1] for _, g in members) - buffer_m
        maxx = max(g.bounds[2] for _, g in members) + buffer_m
        maxy = max(g.bounds[3] for _, g in members) + buffer_m
        x0, y0 = _snap_down(minx, BLOCK_SNAP_M), _snap_down(miny, BLOCK_SNAP_M)
        x1, y1 = _snap_up(maxx, BLOCK_SNAP_M), _snap_up(maxy, BLOCK_SNAP_M)
        # 大簇切成 side×side 的方格（原点同样对齐），只保留与地块相交的方格
        if (x1 - x0) * (y1 - y0) / RESOLUTION_M**2 > max_pixels:
            tiles = [
                (float(tx), float(max(y0, ty - side)), float(min(x1, tx + side)), float(ty))
                for tx in np.arange(x0, x1, side)
                for ty in np.arange(y1, y0, -side)
            ]
        else:
            tiles = [(x0, y0, x1, y1)]
        for tx, ty0, tx1, ty in tiles:
            tile = box(tx, ty0, tx1, ty)
            sel = [(lid, g) for lid, g in members if g.buffer(buffer_m).intersects(tile)]
            if not sel:
                continue
            epsg = crs.split(":")[-1]
            key = f"e{epsg}_x{int(tx)}_y{int(ty)}"
            blk = blocks.get(key)
            if blk is None:
                blk = Block(
                    key=key,
                    crs=crs,
                    x0=float(tx),
                    y1=float(ty),
                    width=int(round((tx1 - tx) / RESOLUTION_M)),
                    height=int(round((ty - ty0) / RESOLUTION_M)),
                )
                blocks[key] = blk
            for lid, g in sel:
                if lid not in blk.land_ids:
                    blk.land_ids.append(lid)
                    blk.geoms.append(g)
    return sorted(blocks.values(), key=lambda b: b.key)


def parcel_mask(block: Block, buffer_m: float | None = None) -> np.ndarray:
    """块网格上的保留掩膜：地块外扩 ``buffer_m`` 内为 True。"""
    from rasterio.features import geometry_mask
    from shapely.geometry import mapping

    buffer_m = mask_buffer_m() if buffer_m is None else buffer_m
    shapes = [mapping(g.buffer(buffer_m) if buffer_m > 0 else g) for g in block.geoms]
    if not shapes:
        return np.zeros((block.height, block.width), dtype=bool)
    return geometry_mask(
        shapes,
        out_shape=(block.height, block.width),
        transform=block.transform,
        invert=True,
        all_touched=True,
    )


def band_radiometry(item_properties: Mapping[str, Any], asset: Mapping[str, Any]) -> tuple[float, float]:
    """返回 (scale, offset)；Earth Search 已扣 BOA 偏移的条目 offset 记为 0（同 ADR-20261009）。"""
    rb = (asset.get("raster:bands") or [{}])[0] or {}
    scale = float(rb.get("scale", 1e-4) or 1e-4)
    offset = 0.0 if item_properties.get("earthsearch:boa_offset_applied") else float(rb.get("offset", 0.0) or 0.0)
    return scale, offset


def to_uint16_reflectance(dn: np.ndarray, scale: float, offset: float) -> np.ndarray:
    """DN→反射率×10000 的 uint16；NaN/0 视为无数据写 0，有效但 ≤0 的反射率钳到 1。"""
    arr = np.asarray(dn, dtype=np.float32)
    valid = np.isfinite(arr) & (arr != 0)
    with np.errstate(invalid="ignore", over="ignore"):
        refl = (arr * np.float32(scale) + np.float32(offset)) * np.float32(10000.0)
    out = np.zeros(arr.shape, dtype=np.uint16)
    out[valid] = np.clip(np.rint(refl[valid]), 1, 65535).astype(np.uint16)
    return out


def to_uint16_scl(scl: np.ndarray) -> np.ndarray:
    arr = np.asarray(scl, dtype=np.float32)
    out = np.zeros(arr.shape, dtype=np.uint16)
    ok = np.isfinite(arr)
    out[ok] = np.clip(np.rint(arr[ok]), 0, 255).astype(np.uint16)
    return out


def mosaic_fill(acc: np.ndarray | None, new: np.ndarray) -> np.ndarray:
    """同日多景拼接：已有值（非 0）保留，空位用后续景补齐。按 (band, y, x) 逐像元。"""
    if acc is None:
        return new.copy()
    # 以 SCL 之外任一光谱波段是否有值判断像元已被占用，保证同一像元的各波段来自同一景
    filled = (acc[:-1] != 0).any(axis=0)
    take = ~filled & (new[:-1] != 0).any(axis=0)
    acc[:, take] = new[:, take]
    return acc


def write_cog(
    path: str,
    stack: np.ndarray,
    block: Block,
    band_names: Iterable[str] = BAND_NAMES,
    *,
    tags: Mapping[str, str] | None = None,
) -> None:
    """把 (bands, H, W) uint16 写成 ZSTD/predictor2/256 瓦片/带概视图的 COG。"""
    from rasterio.io import MemoryFile
    from rio_cogeo.cogeo import cog_translate
    from rio_cogeo.profiles import cog_profiles

    names = list(band_names)
    if stack.dtype != np.uint16 or stack.ndim != 3 or stack.shape[0] != len(names):
        raise ValueError("stack must be uint16 (bands, H, W) matching band_names")
    profile = {
        "driver": "GTiff",
        "dtype": "uint16",
        "count": stack.shape[0],
        "height": stack.shape[1],
        "width": stack.shape[2],
        "crs": block.crs,
        "transform": block.transform,
        "nodata": 0,
    }
    out_profile = cog_profiles.get("zstd")
    out_profile.update(blockxsize=256, blockysize=256, predictor=2)
    with MemoryFile() as mem:
        with mem.open(**profile) as ds:
            ds.write(stack)
            for i, n in enumerate(names, start=1):
                ds.set_band_description(i, n)
            ds.update_tags(**{k: str(v) for k, v in (tags or {}).items()})
        with mem.open() as src:
            cog_translate(
                src,
                path,
                out_profile,
                overview_resampling="nearest",
                in_memory=True,
                quiet=True,
                add_mask=False,
            )
