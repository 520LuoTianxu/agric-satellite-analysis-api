"""Shared windowed-COG GDAL/HTTP tunables + grep-friendly TIMING logs.

Bench (Element84 AWS, 2026-11-02): windowed /vsicurl + these Env knobs beat
default GDAL ~4.5× on single-band S2 reads. Apply via ``rasterio.Env(**env)``
for both S2 and S1 so process-level OSS/GDAL work is not permanently mutated.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

_log = logging.getLogger("openfarm.ingest.windowed_gdal")

_DEFAULT_CURL_CHUNK_SIZE = 262144  # 256 KiB
_DEFAULT_CURL_CACHE_SIZE = 64 * 1024 * 1024
_DEFAULT_VSI_CACHE_SIZE = 64 * 1024 * 1024
_DEFAULT_INGESTED_BYTES = 32768

_JP2_SUFFIXES = (".jp2", ".JP2", ".j2k", ".J2K", ".jpx", ".JPX")


def _positive_int_env(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def windowed_cog_gdal_env(*, prefix: str | None = None) -> dict[str, str]:
    """Winning AWS windowed-COG vsicurl Env (bench B3 / full pull).

    ``prefix`` selects optional env overrides:
    - ``None`` / ``""``: ``WINDOWED_GDAL_*`` then bare ``GDAL_*`` / ``CPL_*``
    - ``"S1"``: ``S1_GDAL_*`` / ``S1_VSI_*`` first (compat with existing S1 knobs)
    """
    p = (prefix or "").strip().upper()

    def _pick_int(*names: str, default: int, minimum: int) -> int:
        for name in names:
            if os.environ.get(name) is not None and str(os.environ.get(name)).strip() != "":
                return _positive_int_env(name, default, minimum=minimum)
        return default

    def _pick_str(*names: str, default: str) -> str:
        for name in names:
            raw = os.environ.get(name)
            if raw is not None and str(raw).strip() != "":
                return str(raw).strip()
        return default

    if p == "S1":
        chunk = _pick_int(
            "S1_GDAL_CURL_CHUNK_SIZE",
            "WINDOWED_GDAL_CURL_CHUNK_SIZE",
            default=_DEFAULT_CURL_CHUNK_SIZE,
            minimum=16 * 1024,
        )
        curl_cache = _pick_int(
            "S1_GDAL_CURL_CACHE_SIZE",
            "WINDOWED_GDAL_CURL_CACHE_SIZE",
            default=_DEFAULT_CURL_CACHE_SIZE,
            minimum=1024 * 1024,
        )
        vsi_cache = _pick_int(
            "S1_VSI_CACHE_SIZE",
            "WINDOWED_VSI_CACHE_SIZE",
            default=_DEFAULT_VSI_CACHE_SIZE,
            minimum=1024 * 1024,
        )
        http_version = _pick_str(
            "S1_GDAL_HTTP_VERSION", "WINDOWED_GDAL_HTTP_VERSION", default="2"
        )
        multirange = _pick_str(
            "S1_GDAL_HTTP_MULTIRANGE", "WINDOWED_GDAL_HTTP_MULTIRANGE", default="YES"
        )
    else:
        chunk = _pick_int(
            "WINDOWED_GDAL_CURL_CHUNK_SIZE",
            "GDAL_CURL_CHUNK_SIZE",
            default=_DEFAULT_CURL_CHUNK_SIZE,
            minimum=16 * 1024,
        )
        curl_cache = _pick_int(
            "WINDOWED_GDAL_CURL_CACHE_SIZE",
            "CPL_VSIL_CURL_CACHE_SIZE",
            default=_DEFAULT_CURL_CACHE_SIZE,
            minimum=1024 * 1024,
        )
        vsi_cache = _pick_int(
            "WINDOWED_VSI_CACHE_SIZE",
            "VSI_CACHE_SIZE",
            default=_DEFAULT_VSI_CACHE_SIZE,
            minimum=1024 * 1024,
        )
        http_version = _pick_str(
            "WINDOWED_GDAL_HTTP_VERSION", "GDAL_HTTP_VERSION", default="2"
        )
        multirange = _pick_str(
            "WINDOWED_GDAL_HTTP_MULTIRANGE", "GDAL_HTTP_MULTIRANGE", default="YES"
        )

    ingested = _pick_int(
        "WINDOWED_GDAL_INGESTED_BYTES_AT_OPEN",
        "GDAL_INGESTED_BYTES_AT_OPEN",
        default=_DEFAULT_INGESTED_BYTES,
        minimum=0,
    )
    connect_timeout = _pick_str(
        "GDAL_HTTP_CONNECTTIMEOUT", default="10"
    )
    http_timeout = _pick_str("GDAL_HTTP_TIMEOUT", default="60")
    low_speed_limit = _pick_str("GDAL_HTTP_LOW_SPEED_LIMIT", default="1")
    low_speed_time = _pick_str("GDAL_HTTP_LOW_SPEED_TIME", default="30")
    # Keep GDAL-internal retries low; band_parallel already retries.
    max_retry = _pick_str("GDAL_HTTP_MAX_RETRY", default="1")
    retry_delay = _pick_str("GDAL_HTTP_RETRY_DELAY", default="1")

    env: dict[str, str] = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "GDAL_HTTP_UNSAFESSL": "NO",
        "CPL_VSIL_CURL_USE_HEAD": "NO",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF,.tiff,.TIFF",
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "GDAL_HTTP_MULTIPLEX": "YES",
        "GDAL_HTTP_MULTIRANGE": multirange,
        "GDAL_HTTP_VERSION": http_version,
        "GDAL_INGESTED_BYTES_AT_OPEN": str(ingested),
        "CPL_VSIL_CURL_CHUNK_SIZE": str(chunk),
        "CPL_VSIL_CURL_CACHE_SIZE": str(curl_cache),
        "VSI_CACHE": "TRUE",
        "VSI_CACHE_SIZE": str(vsi_cache),
        "GDAL_HTTP_TCP_KEEPALIVE": "YES",
        "GDAL_HTTP_TCP_KEEPIDLE": "30",
        "GDAL_HTTP_TCP_KEEPINTVL": "15",
        "GDAL_HTTP_CONNECTTIMEOUT": connect_timeout,
        "GDAL_HTTP_TIMEOUT": http_timeout,
        "GDAL_HTTP_LOW_SPEED_LIMIT": low_speed_limit,
        "GDAL_HTTP_LOW_SPEED_TIME": low_speed_time,
        "GDAL_HTTP_MAX_RETRY": max_retry,
        "GDAL_HTTP_RETRY_DELAY": retry_delay,
        # Anonymous AWS Open Data Range reads (S2 Element84 + S1 sentinel-s1-l1c).
        "AWS_NO_SIGN_REQUEST": "YES",
        "GDAL_NUM_THREADS": "1",
    }
    return env


def is_non_cog_jp2_href(href: str | None) -> bool:
    """True when the asset URL clearly points at JPEG2000 (not windowable COG)."""
    if not href:
        return False
    raw = str(href).strip()
    for prefix in ("/vsicurl/", "/vsis3/", "/vsiaz/", "/vsis/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    path = urlsplit(raw).path or raw
    return any(path.endswith(suf) for suf in _JP2_SUFFIXES)


def asset_is_non_cog_jp2(asset: Any) -> bool:
    """STAC asset looks like JP2 by href or media_type / type."""
    if asset is None:
        return False
    href = getattr(asset, "href", None)
    if href is None and isinstance(asset, dict):
        href = asset.get("href")
    if is_non_cog_jp2_href(href if href is None else str(href)):
        return True
    media = getattr(asset, "media_type", None)
    if media is None and isinstance(asset, dict):
        media = asset.get("type") or asset.get("media_type")
    text = str(media or "").lower()
    return "jp2" in text or "jpeg2000" in text or "jpx" in text


def log_timing(stage: str, *, level: str = "info", **fields: Any) -> None:
    """Emit one grep-friendly ``TIMING stage=...`` line plus structlog event.

    Docker/log shippers can ``grep 'TIMING stage='`` then aggregate by stage.
    Fields stay flat (land_id / scene_id / sensor / attempt / *_ms).
    """
    stage_s = str(stage).strip() or "unknown"
    flat: dict[str, Any] = {"stage": stage_s}
    for key, value in fields.items():
        if value is None:
            continue
        flat[key] = value

    parts = [f"TIMING stage={stage_s}"]
    for key, value in flat.items():
        if key == "stage":
            continue
        if isinstance(value, float):
            parts.append(f"{key}={value:.3f}")
        else:
            parts.append(f"{key}={value}")
    line = " ".join(parts)

    try:
        import structlog

        logger = structlog.get_logger()
        getattr(logger, level, logger.info)("TIMING", message=line, **flat)
    except Exception:
        getattr(_log, level, _log.info)(line)


@contextmanager
def timing_span(stage: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Context manager that records wall_ms and logs ``TIMING`` on exit."""
    meta: dict[str, Any] = dict(fields)
    t0 = time.perf_counter()
    try:
        yield meta
        meta.setdefault("outcome", "ok")
    except Exception as exc:
        meta.setdefault("outcome", "error")
        meta.setdefault("error_type", type(exc).__name__)
        meta.setdefault("error", str(exc)[:300])
        raise
    finally:
        meta["wall_ms"] = int((time.perf_counter() - t0) * 1000)
        log_timing(stage, **meta)


def apply_process_windowed_gdal_defaults() -> None:
    """setdefault process env so empty ``rasterio.Env()`` still gets fast knobs.

    Prefer passing ``windowed_cog_gdal_env()`` into ``rasterio.Env`` explicitly;
    this is a safety net for call sites that open with a bare Env.
    """
    for key, value in windowed_cog_gdal_env().items():
        os.environ.setdefault(key, value)


__all__ = [
    "apply_process_windowed_gdal_defaults",
    "asset_is_non_cog_jp2",
    "is_non_cog_jp2_href",
    "log_timing",
    "timing_span",
    "windowed_cog_gdal_env",
]
