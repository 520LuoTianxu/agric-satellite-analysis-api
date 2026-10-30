"""Lonlat parcel scene pixel encoding: object-list (legacy) and columnar JSON.

New writes use ``format=lonlat_columns_v1`` with equal-length ``columns`` arrays
so field names are not repeated per pixel. Readers must accept both:

- ``lonlat_v1``: ``{"format":"lonlat_v1","pixels":[{lon,lat,...}, ...]}``
- ``lonlat_columns_v1``: ``{"format":"lonlat_columns_v1","columns":{"lon":[...],...}}``

Constraints: same pixel count and order; JSON numbers keep full float precision
(no float32 quantization); stored product is plain JSON (no gzip wrapper).
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence

# Legacy object-list encoding (still readable forever).
LONLAT_FORMAT_V1 = "lonlat_v1"
# Columnar encoding for new OSS/DB writes.
LONLAT_FORMAT_COLUMNS_V1 = "lonlat_columns_v1"

LONLAT_FORMATS = frozenset({LONLAT_FORMAT_V1, LONLAT_FORMAT_COLUMNS_V1})

# Preferred column order for optical + S1 products (extras append after).
_PREFERRED_COLUMN_ORDER: tuple[str, ...] = (
    "lon",
    "lat",
    "clear",
    "NDVI",
    "EVI",
    "NDMI",
    "NDRE",
    "CIre",
    "MNDWI",
    "VV_db",
    "VH_db",
)

# SQL: accept either format when filtering parcel_scene_products rows.
SQL_LONLAT_FORMAT_IN = (
    f"pixel_data->>'format' IN ('{LONLAT_FORMAT_V1}', '{LONLAT_FORMAT_COLUMNS_V1}')"
)

# SQL: row has a non-empty lonlat pixel payload (object list or columnar lon).
SQL_LONLAT_NONEMPTY = f"""(
  (pixel_data->>'format' = '{LONLAT_FORMAT_V1}'
   AND jsonb_typeof(pixel_data->'pixels') = 'array'
   AND jsonb_array_length(pixel_data->'pixels') > 0)
  OR
  (pixel_data->>'format' = '{LONLAT_FORMAT_COLUMNS_V1}'
   AND jsonb_typeof(pixel_data->'columns'->'lon') = 'array'
   AND jsonb_array_length(pixel_data->'columns'->'lon') > 0)
)"""

# SQL: pixel count from either encoding (0 when unknown/empty).
SQL_LONLAT_PIXEL_COUNT = f"""CASE
  WHEN pixel_data->>'format' = '{LONLAT_FORMAT_COLUMNS_V1}'
       AND jsonb_typeof(pixel_data->'columns'->'lon') = 'array'
  THEN jsonb_array_length(pixel_data->'columns'->'lon')
  WHEN pixel_data->>'format' = '{LONLAT_FORMAT_V1}'
       AND jsonb_typeof(pixel_data->'pixels') = 'array'
  THEN jsonb_array_length(pixel_data->'pixels')
  ELSE 0
END"""


def is_lonlat_format(fmt: Any) -> bool:
    """True when *fmt* is a known lonlat pixel payload format string."""
    return isinstance(fmt, str) and fmt in LONLAT_FORMATS


def is_lonlat_pixel_data(pixel_data: Any) -> bool:
    """True when *pixel_data* looks like lonlat_v1 or lonlat_columns_v1."""
    return isinstance(pixel_data, dict) and is_lonlat_format(pixel_data.get("format"))


def pixel_count(pixel_data: Any) -> int:
    """Number of pixels in a lonlat payload (0 if missing/invalid)."""
    if not isinstance(pixel_data, dict):
        return 0
    fmt = pixel_data.get("format")
    if fmt == LONLAT_FORMAT_COLUMNS_V1:
        columns = pixel_data.get("columns")
        if not isinstance(columns, dict):
            return 0
        lon = columns.get("lon")
        if isinstance(lon, list):
            return len(lon)
        # Fall back to first list column length.
        for values in columns.values():
            if isinstance(values, list):
                return len(values)
        return 0
    pixels = pixel_data.get("pixels")
    if isinstance(pixels, list):
        return len(pixels)
    return 0


def pixels_to_columns(pixels: Sequence[Mapping[str, Any]]) -> dict[str, list[Any]]:
    """Convert object-list pixels to equal-length columnar arrays.

    Missing keys on a pixel become JSON ``null`` (Python ``None``) so every
    column has the same length. Round-tripping via :func:`columns_to_pixels`
    drops nulls again, restoring sparse object keys.
    """
    if not pixels:
        return {"lon": [], "lat": []}

    present: set[str] = set()
    for pix in pixels:
        if isinstance(pix, Mapping):
            present.update(pix.keys())

    key_order: list[str] = [k for k in _PREFERRED_COLUMN_ORDER if k in present]
    extras = sorted(k for k in present if k not in key_order)
    key_order.extend(extras)
    if "lon" not in key_order:
        key_order.insert(0, "lon")
    if "lat" not in key_order:
        key_order.insert(1 if key_order and key_order[0] == "lon" else 0, "lat")

    columns: dict[str, list[Any]] = {k: [] for k in key_order}
    for pix in pixels:
        if not isinstance(pix, Mapping):
            for k in key_order:
                columns[k].append(None)
            continue
        for k in key_order:
            columns[k].append(pix[k] if k in pix else None)
    return columns


def columns_to_pixels(columns: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expand columnar arrays back to an object-list (nulls omitted)."""
    if not isinstance(columns, Mapping) or not columns:
        return []

    keys = [k for k, values in columns.items() if isinstance(values, list)]
    if not keys:
        return []

    n = len(columns[keys[0]])
    for k in keys[1:]:
        if len(columns[k]) != n:
            raise ValueError(
                f"columnar lonlat length mismatch: {keys[0]}={n} vs {k}={len(columns[k])}"
            )

    out: list[dict[str, Any]] = []
    for i in range(n):
        pix: dict[str, Any] = {}
        for k in keys:
            value = columns[k][i]
            if value is not None:
                pix[k] = value
        out.append(pix)
    return out


def extract_pixels(pixel_data: Any) -> list[dict[str, Any]]:
    """Return object-list pixels from either lonlat encoding (or empty list).

    Also accepts a bare list of pixel dicts (legacy callers / OSS root ``pixels``).
    Does not validate WGS84 ranges — callers that need sanitization should run
    their own normalizer afterward.
    """
    if isinstance(pixel_data, list):
        return [dict(p) for p in pixel_data if isinstance(p, Mapping)]
    if not isinstance(pixel_data, dict):
        return []

    fmt = pixel_data.get("format")
    if fmt == LONLAT_FORMAT_COLUMNS_V1 or (
        fmt is None and isinstance(pixel_data.get("columns"), dict)
    ):
        columns = pixel_data.get("columns")
        if isinstance(columns, dict):
            return columns_to_pixels(columns)
        return []

    pixels = pixel_data.get("pixels")
    if isinstance(pixels, list):
        return [dict(p) for p in pixels if isinstance(p, Mapping)]
    return []


def iter_pixels(pixel_data: Any) -> Iterator[dict[str, Any]]:
    """Iterate object-list pixels without materializing twice when already a list."""
    if isinstance(pixel_data, list):
        for p in pixel_data:
            if isinstance(p, Mapping):
                yield dict(p)
        return
    yield from extract_pixels(pixel_data)


def to_columnar_pixel_data(
    pixels: Sequence[Mapping[str, Any]],
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``lonlat_columns_v1`` pixel_data object from an object-list.

    *extra* keys (source, algorithm_version, …) are shallow-copied onto the
    result; ``format`` / ``pixels`` / ``columns`` in *extra* are ignored.
    """
    payload: dict[str, Any] = {
        "format": LONLAT_FORMAT_COLUMNS_V1,
        "columns": pixels_to_columns(pixels),
    }
    if extra:
        for key, value in extra.items():
            if key in {"format", "pixels", "columns"}:
                continue
            payload[key] = value
    return payload


def replace_pixels_with_columns(
    pixel_data: MutableMapping[str, Any],
    pixels: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """In-place upgrade of a pixel_data dict to columnar encoding.

    If *pixels* is omitted, existing ``pixels`` (or already-columnar columns)
    are used. Returns the same mapping for chaining.
    """
    if pixels is None:
        pixels = extract_pixels(pixel_data)
    columns = pixels_to_columns(pixels)
    pixel_data["format"] = LONLAT_FORMAT_COLUMNS_V1
    pixel_data["columns"] = columns
    pixel_data.pop("pixels", None)
    return dict(pixel_data)


def extract_pixels_from_product(obj: Any) -> list[dict[str, Any]]:
    """Pull pixels from a scene product JSON (nested ``pixel_data`` or root).

    Prefer nested ``pixel_data``; fall back to root ``columns`` / ``pixels`` for
    older OSS objects that flattened the payload.
    """
    if not isinstance(obj, dict):
        return []
    nested = obj.get("pixel_data")
    if isinstance(nested, dict) and (
        is_lonlat_format(nested.get("format"))
        or "pixels" in nested
        or "columns" in nested
    ):
        got = extract_pixels(nested)
        if got:
            return got
    if "columns" in obj or obj.get("format") in LONLAT_FORMATS:
        got = extract_pixels(obj)
        if got:
            return got
    raw = obj.get("pixels")
    if isinstance(raw, list):
        return [dict(p) for p in raw if isinstance(p, Mapping)]
    return []


def roundtrip_ok(
    pixels: Sequence[Mapping[str, Any]],
) -> bool:
    """True when columnar encode→decode preserves length, keys, and values."""
    columns = pixels_to_columns(pixels)
    back = columns_to_pixels(columns)
    if len(back) != len(pixels):
        return False
    for original, restored in zip(pixels, back):
        if not isinstance(original, Mapping):
            return False
        if set(restored.keys()) != set(original.keys()):
            return False
        for key, value in original.items():
            if restored.get(key) != value:
                return False
    return True


__all__ = [
    "LONLAT_FORMAT_V1",
    "LONLAT_FORMAT_COLUMNS_V1",
    "LONLAT_FORMATS",
    "SQL_LONLAT_FORMAT_IN",
    "SQL_LONLAT_NONEMPTY",
    "SQL_LONLAT_PIXEL_COUNT",
    "is_lonlat_format",
    "is_lonlat_pixel_data",
    "pixel_count",
    "pixels_to_columns",
    "columns_to_pixels",
    "extract_pixels",
    "iter_pixels",
    "to_columnar_pixel_data",
    "replace_pixels_with_columns",
    "extract_pixels_from_product",
    "roundtrip_ok",
]
