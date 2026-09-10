"""Vegetation index registry - formulas, bands, colormaps, alert defaults.

Every Celery task, API endpoint, and tile URL builder references this
registry so index behaviour is defined in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np


# ── Formula functions ────────────────────────────────────────────────


def _ndvi(bands: dict[str, np.ndarray], **_kw) -> np.ndarray:
    nir, red = bands["B08"], bands["B04"]
    with np.errstate(divide="ignore", invalid="ignore"):
        result = (nir - red) / (nir + red)
    return np.clip(result, -1.0, 1.0)


def _evi(bands: dict[str, np.ndarray], **_kw) -> np.ndarray:
    """Enhanced Vegetation Index (Huete et al.).

    Storage clip is [-1, 2] (not [-1, 1]): dense canopy / high LAI scenes
    routinely exceed 1.0; hard-clipping to 1.0 saturates float COGs and
    stuck parcel means at 1.000. TiTiler / UI rescale separately.
    """
    nir, red, blue = bands["B08"], bands["B04"], bands["B02"]
    with np.errstate(divide="ignore", invalid="ignore"):
        result = 2.5 * (nir - red) / (nir + 6.0 * red - 7.5 * blue + 1.0)
    return np.clip(result, -1.0, 2.0)


def _savi(bands: dict[str, np.ndarray], **_kw) -> np.ndarray:
    nir, red = bands["B08"], bands["B04"]
    L = _kw.get("savi_l", 0.5)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = ((nir - red) / (nir + red + L)) * (1.0 + L)
    return np.clip(result, -1.5, 1.5)


def _ndwi(bands: dict[str, np.ndarray], **_kw) -> np.ndarray:
    green, nir = bands["B03"], bands["B08"]
    with np.errstate(divide="ignore", invalid="ignore"):
        result = (green - nir) / (green + nir)
    return np.clip(result, -1.0, 1.0)


def _ndmi(bands: dict[str, np.ndarray], **_kw) -> np.ndarray:
    """Normalized Difference Moisture Index: (NIR - SWIR16) / (NIR + SWIR16)."""
    nir, swir = bands["B08"], bands["B11"]
    with np.errstate(divide="ignore", invalid="ignore"):
        result = (nir - swir) / (nir + swir)
    return np.clip(result, -1.0, 1.0)


def _ndre(bands: dict[str, np.ndarray], **_kw) -> np.ndarray:
    """Normalized Difference Red Edge: (NIR - RedEdge1) / (NIR + RedEdge1)."""
    nir, re1 = bands["B08"], bands["B05"]
    with np.errstate(divide="ignore", invalid="ignore"):
        result = (nir - re1) / (nir + re1)
    return np.clip(result, -1.0, 1.0)


def _cire(bands: dict[str, np.ndarray], **_kw) -> np.ndarray:
    """Chlorophyll Index red-edge: (RedEdge3 / RedEdge1) - 1  (B07/B05 - 1)."""
    re3, re1 = bands["B07"], bands["B05"]
    with np.errstate(divide="ignore", invalid="ignore"):
        result = (re3 / re1) - 1.0
    # Physical range is open-ended; keep a wide float bound for COG storage.
    return np.clip(result, -1.0, 10.0)


def _mndwi(bands: dict[str, np.ndarray], **_kw) -> np.ndarray:
    """Modified NDWI (Xu 2006): (Green - SWIR16) / (Green + SWIR16)."""
    green, swir = bands["B03"], bands["B11"]
    with np.errstate(divide="ignore", invalid="ignore"):
        result = (green - swir) / (green + swir)
    return np.clip(result, -1.0, 1.0)


# ── Alert configuration ─────────────────────────────────────────────


@dataclass(frozen=True)
class AlertDefaults:
    threshold: float
    threshold_high: float  # below this → high severity (else medium)
    drop_pct: float
    drop_window: int = 4


# ── Index definition ─────────────────────────────────────────────────


@dataclass(frozen=True)
class IndexDef:
    """Complete definition for one vegetation index."""

    key: str  # lowercase, used in COG paths and job types
    label: str  # uppercase, stored in raster_layers.layer_type
    bands: tuple[str, ...]  # Sentinel-2 asset keys required
    formula: Callable[[dict[str, np.ndarray]], np.ndarray]
    colormap: str  # TiTiler colormap name
    rescale: tuple[float, float]  # min, max for tile rendering
    alerts: AlertDefaults
    stac_asset_map: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # stac_asset_map: band_key → (primary_asset, fallback_asset, ...)


# ── Registry ─────────────────────────────────────────────────────────

INDEX_REGISTRY: dict[str, IndexDef] = {}


def _register(idx: IndexDef) -> None:
    INDEX_REGISTRY[idx.key] = idx


_register(
    IndexDef(
        key="ndvi",
        label="NDVI",
        bands=("B04", "B08"),
        formula=_ndvi,
        colormap="rdylgn",
        rescale=(-0.2, 0.9),
        alerts=AlertDefaults(threshold=0.3, threshold_high=0.15, drop_pct=15),
        stac_asset_map={
            "B04": ("red", "B04"),
            "B08": ("nir", "B08"),
        })
)

_register(
    IndexDef(
        key="evi",
        label="EVI",
        bands=("B02", "B04", "B08"),
        formula=_evi,
        colormap="rdylgn",
        # Display rescale allows values >1 after storage clip widened to [-1, 2]
        rescale=(-0.2, 1.2),
        alerts=AlertDefaults(threshold=0.2, threshold_high=0.1, drop_pct=15),
        stac_asset_map={
            "B02": ("blue", "B02"),
            "B04": ("red", "B04"),
            "B08": ("nir", "B08"),
        })
)

_register(
    IndexDef(
        key="savi",
        label="SAVI",
        bands=("B04", "B08"),
        formula=_savi,
        colormap="rdylgn",
        rescale=(-0.2, 0.8),
        alerts=AlertDefaults(threshold=0.25, threshold_high=0.1, drop_pct=15),
        stac_asset_map={
            "B04": ("red", "B04"),
            "B08": ("nir", "B08"),
        })
)

_register(
    IndexDef(
        key="ndwi",
        label="NDWI",
        bands=("B03", "B08"),
        formula=_ndwi,
        colormap="rdbu",
        rescale=(-1.0, 1.0),
        alerts=AlertDefaults(threshold=0.0, threshold_high=-0.2, drop_pct=20),
        stac_asset_map={
            "B03": ("green", "B03"),
            "B08": ("nir", "B08"),
        })
)

_register(
    IndexDef(
        key="ndmi",
        label="NDMI",
        bands=("B08", "B11"),
        formula=_ndmi,
        colormap="rdylgn",
        rescale=(-0.5, 0.5),
        alerts=AlertDefaults(threshold=0.0, threshold_high=-0.2, drop_pct=20),
        stac_asset_map={
            "B08": ("nir", "B08"),
            "B11": ("swir16", "B11"),
        })
)

_register(
    IndexDef(
        key="ndre",
        label="NDRE",
        bands=("B05", "B08"),
        formula=_ndre,
        colormap="rdylgn",
        rescale=(-0.2, 0.8),
        alerts=AlertDefaults(threshold=0.2, threshold_high=0.1, drop_pct=15),
        stac_asset_map={
            "B05": ("rededge1", "B05"),
            "B08": ("nir", "B08"),
        })
)

_register(
    IndexDef(
        key="cire",
        label="CIRE",
        bands=("B05", "B07"),
        formula=_cire,
        colormap="rdylgn",
        rescale=(0.0, 1.5),
        alerts=AlertDefaults(threshold=0.2, threshold_high=0.1, drop_pct=15),
        stac_asset_map={
            "B05": ("rededge1", "B05"),
            "B07": ("rededge3", "B07"),
        })
)

_register(
    IndexDef(
        key="mndwi",
        label="MNDWI",
        bands=("B03", "B11"),
        formula=_mndwi,
        colormap="rdbu",
        rescale=(-0.5, 0.5),
        alerts=AlertDefaults(threshold=0.0, threshold_high=-0.2, drop_pct=20),
        stac_asset_map={
            "B03": ("green", "B03"),
            "B11": ("swir16", "B11"),
        })
)


def get_index(key: str) -> IndexDef:
    """Look up an index definition; raises ValueError if unknown."""
    try:
        return INDEX_REGISTRY[key.lower()]
    except KeyError:
        valid = ", ".join(sorted(INDEX_REGISTRY))
        raise ValueError(f"Unknown index '{key}'. Valid: {valid}")


VALID_INDEX_KEYS = tuple(sorted(INDEX_REGISTRY.keys()))

# Derive task names from the index registry - single source of truth.
# The ndvi task lives in its own legacy module; others share the vegetation module.
INDEX_TASK_MAP: dict[str, str] = {
    key: f"app.tasks.{'ndvi' if key == 'ndvi' else 'vegetation'}.process_{key}"
    for key in INDEX_REGISTRY
}
