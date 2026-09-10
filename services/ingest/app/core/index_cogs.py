"""Env knobs for index COG uploads vs agri lonlat-direct emit.

WRITE_INDEX_COGS (unset = safe default):
  agri fields: off (do not upload index .tif / COG products)
  classic OpenFarm fields: on (TiTiler still needs COGs)

Explicit values:
  0 / false / off / no  -> never upload index TIFs
  1 / true  / on  / yes -> always upload, including agri (opt-in)

UPLOAD_SCENE_JSON (default on): compact lonlat_v1 scene JSON to OSS_PREFIX.
This is small JSON, not a raster. Set 0 to skip the JSON object.
"""

from __future__ import annotations

import os


def _parse_bool_env(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value == "":
        return None
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


def write_index_cogs_enabled(*, is_agri: bool = False) -> bool:
    """Whether to upload full index COG/TIF rasters to object storage."""
    explicit = _parse_bool_env("WRITE_INDEX_COGS")
    if explicit is not None:
        return explicit
    return not is_agri


def upload_scene_json_enabled() -> bool:
    """Whether to upload compact lonlat_v1 scene JSON (not rasters)."""
    explicit = _parse_bool_env("UPLOAD_SCENE_JSON")
    if explicit is not None:
        return explicit
    return True
