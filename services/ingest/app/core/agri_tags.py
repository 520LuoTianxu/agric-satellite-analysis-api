"""Helpers for agric-satellite-analysis agri-tagged field records.

Agri-first data plane:
- Remote sensing truth lives in agric_satellite.parcel_scene_products (lonlat_v1).
- Field records may carry tags like ``agri:<land_id>`` so soil/weather
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


def parse_cdfinance_group_id(tags: Any) -> str | None:
    """Return group id from ``cdfinance_group:<id>`` / ``group:<id>`` tags."""
    for tag in iter_tag_strings(tags):
        for prefix in ("cdfinance_group:", "group:"):
            if tag.startswith(prefix):
                gid = tag[len(prefix) :].strip()
                if gid:
                    return gid
    return None


def ensure_cdfinance_group_tag(tags: Any, group_id: str | int) -> list[str]:
    """Return tags list with ``cdfinance_group:<id>`` present (idempotent)."""
    gid = str(group_id).strip()
    out = list(iter_tag_strings(tags))
    marker = f"cdfinance_group:{gid}"
    if marker not in out and f"group:{gid}" not in out:
        out.append(marker)
    return out


def ensure_agri_land_tag(tags: Any, land_id: str | int | None) -> list[str]:
    """Replace any ``agri:*`` tag with ``agri:<land_id>``, or drop agri tags if empty.

    Idempotent for the same land_id. Does not require ``agric_satellite.land_parcels`` to exist.
    """
    out = [t for t in iter_tag_strings(tags) if not t.startswith("agri:")]
    if land_id is None:
        return out
    lid = str(land_id).strip()
    if not lid:
        return out
    out.append(f"agri:{lid}")
    return out
