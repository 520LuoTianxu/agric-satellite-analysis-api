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

def sql_lonlat_format_in(col: str = "pixel_data") -> str:
    """SQL predicate: *col* format is lonlat_v1 or lonlat_columns_v1.

    *col* may be ``pixel_data`` or an aliased expression such as
    ``scene.pixel_data``.
    """
    return (
        f"{col}->>'format' IN ('{LONLAT_FORMAT_V1}', '{LONLAT_FORMAT_COLUMNS_V1}')"
    )


def sql_lonlat_nonempty(col: str = "pixel_data") -> str:
    """SQL predicate: *col* has a non-empty lonlat pixel payload."""
    return f"""(
  ({col}->>'format' = '{LONLAT_FORMAT_V1}'
   AND jsonb_typeof({col}->'pixels') = 'array'
   AND jsonb_array_length({col}->'pixels') > 0)
  OR
  ({col}->>'format' = '{LONLAT_FORMAT_COLUMNS_V1}'
   AND jsonb_typeof({col}->'columns'->'lon') = 'array'
   AND jsonb_array_length({col}->'columns'->'lon') > 0)
)"""


def sql_lonlat_pixel_count(col: str = "pixel_data") -> str:
    """SQL expression: pixel count from either encoding (0 when unknown)."""
    return f"""CASE
  WHEN {col}->>'format' = '{LONLAT_FORMAT_COLUMNS_V1}'
       AND jsonb_typeof({col}->'columns'->'lon') = 'array'
  THEN jsonb_array_length({col}->'columns'->'lon')
  WHEN {col}->>'format' = '{LONLAT_FORMAT_V1}'
       AND jsonb_typeof({col}->'pixels') = 'array'
  THEN jsonb_array_length({col}->'pixels')
  ELSE 0
END"""


# Default (unaliased) fragments — prefer these unless the column is aliased.
SQL_LONLAT_FORMAT_IN = sql_lonlat_format_in()
SQL_LONLAT_NONEMPTY = sql_lonlat_nonempty()
SQL_LONLAT_PIXEL_COUNT = sql_lonlat_pixel_count()


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


def needs_columnar_migration(obj: Any) -> bool:
    """True when *obj* still stores lonlat as an object-list (needs convert).

    Accepts either a ``pixel_data`` dict or a full scene product JSON (nested
    ``pixel_data`` and/or root ``pixels``).
    """
    if not isinstance(obj, dict):
        return False
    nested = obj.get("pixel_data")
    if isinstance(nested, dict) and (
        is_lonlat_format(nested.get("format"))
        or "pixels" in nested
        or "columns" in nested
    ):
        return _pixel_data_needs_migration(nested)
    if is_lonlat_format(obj.get("format")) or "pixels" in obj or "columns" in obj:
        # Bare pixel_data, or offline product with root pixels.
        if "land_id" in obj or "scene_id" in obj or "pixel_count" in obj:
            if isinstance(obj.get("pixels"), list) and not (
                obj.get("format") == LONLAT_FORMAT_COLUMNS_V1
                and isinstance(obj.get("columns"), dict)
            ):
                # Root object-list on a scene product.
                if not isinstance(nested, dict) or _pixel_data_needs_migration(nested):
                    return True
        return _pixel_data_needs_migration(obj)
    return False


def _pixel_data_needs_migration(pixel_data: Mapping[str, Any]) -> bool:
    fmt = pixel_data.get("format")
    if fmt == LONLAT_FORMAT_COLUMNS_V1 and isinstance(pixel_data.get("columns"), dict):
        # Already columnar; still migrate if a legacy ``pixels`` key remains.
        return "pixels" in pixel_data
    if fmt == LONLAT_FORMAT_V1:
        return True
    if isinstance(pixel_data.get("pixels"), list) and not isinstance(
        pixel_data.get("columns"), dict
    ):
        return True
    return False


def migrate_pixel_data_to_columnar(
    pixel_data: MutableMapping[str, Any],
) -> bool:
    """In-place convert *pixel_data* to ``lonlat_columns_v1``.

    Preserves meta keys (source, algorithm_version, …), full float values, and
    pixel count/order. Returns True when the mapping was modified.
    """
    if not isinstance(pixel_data, dict):
        return False
    if not _pixel_data_needs_migration(pixel_data):
        return False
    pixels = extract_pixels(pixel_data)
    replace_pixels_with_columns(pixel_data, pixels)
    return True


def migrate_product_to_columnar(
    product: MutableMapping[str, Any],
) -> bool:
    """In-place convert a scene product JSON to columnar ``pixel_data``.

    - Nested ``pixel_data`` (API/ingest shape) is converted in place.
    - Offline products with root ``pixels`` get a nested columnar
      ``pixel_data``; root ``pixels`` / ``columns`` / lonlat ``format`` are
      removed so readers use the nested payload.
    Returns True when anything changed.
    """
    if not isinstance(product, dict):
        return False
    changed = False
    nested = product.get("pixel_data")
    if isinstance(nested, dict) and (
        is_lonlat_format(nested.get("format"))
        or "pixels" in nested
        or "columns" in nested
    ):
        if migrate_pixel_data_to_columnar(nested):
            changed = True
    elif needs_columnar_migration(product) and (
        "land_id" in product or "scene_id" in product or "pixel_count" in product
    ):
        pixels = extract_pixels_from_product(product)
        extra: dict[str, Any] = {}
        for key in (
            "source",
            "algorithm_version",
            "stac_item_id",
            "decloud_quality",
            "parcel_cloud_source",
            "radiometry",
            "analysis_grid",
            "processing_window",
            "quality_metrics",
            "radiometric_calibration",
            "relative_orbit",
        ):
            if key in product and key not in ("format", "pixels", "columns"):
                extra[key] = product[key]
        product["pixel_data"] = to_columnar_pixel_data(pixels, extra=extra or None)
        changed = True

    # Drop duplicate root pixel payload once nested columnar exists.
    nested = product.get("pixel_data")
    if (
        isinstance(nested, dict)
        and nested.get("format") == LONLAT_FORMAT_COLUMNS_V1
        and isinstance(nested.get("columns"), dict)
    ):
        for key in ("pixels", "columns"):
            if key in product:
                product.pop(key, None)
                changed = True
        if product.get("format") in LONLAT_FORMATS:
            product.pop("format", None)
            changed = True
    elif isinstance(nested, dict) is False and _pixel_data_needs_migration(product):
        # Bare pixel_data object passed as *product*.
        if migrate_pixel_data_to_columnar(product):
            changed = True
    return changed



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
    "sql_lonlat_format_in",
    "sql_lonlat_nonempty",
    "sql_lonlat_pixel_count",
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
    "needs_columnar_migration",
    "migrate_pixel_data_to_columnar",
    "migrate_product_to_columnar",
    "roundtrip_ok",
]
