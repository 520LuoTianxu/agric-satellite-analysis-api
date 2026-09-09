"""Helpers for agri-tagged OpenFarm fields.

Agri-first data plane:
- Remote sensing truth lives in agri.parcel_scene_products (lonlat_v1).
- OpenFarm fields may carry tags like ``agri:<land_id>`` so soil/weather
  (keyed by fields.id) bind to the same parcel the UI shows.
"""

from __future__ import annotations

from typing import Any, Iterable


def iter_tag_strings(tags: Any) -> list[str]:
    """Normalize tags_json / list / None into a list of strings."""
    if tags is None:
        return []
    if isinstance(tags, str):
        return [tags]
    if isinstance(tags, dict):
        # unlikely shape; ignore non-list JSON
        return []
    try:
        return [t for t in tags if isinstance(t, str)]
    except TypeError:
        return []


def parse_agri_land_id(tags: Any) -> str | None:
    """Return land_id from the first ``agri:<land_id>`` tag, else None."""
    for tag in iter_tag_strings(tags):
        if tag.startswith("agri:"):
            land_id = tag[5:].strip()
            if land_id:
                return land_id
    return None


def is_agri_tagged(tags: Any) -> bool:
    """True when tags contain an ``agri:<land_id>`` marker."""
    return parse_agri_land_id(tags) is not None


def has_agri_prefix_in_iterable(tags: Iterable[Any] | None) -> bool:
    """Convenience for callers that already have a list."""
    return is_agri_tagged(tags)
