"""Read ingested parcel_scene_products for biweekly generate/list."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import text

from app.core.config import settings
from app.core.logging import logger

# 景的采用规则与气象行的字段换算放在公共包，离线出报工具与本接口共用同一份实现。
# 保留原私有名，供已有观测性测试和其他模块按原名引用。
from agric_satellite_analysis_common.biweekly.scene_rows import (  # noqa: F401
    as_day as _as_day,
    as_float as _as_float,
    as_sensor as _as_sensor,
    eligible_optical as _eligible_optical,
    exclusion_reason as _exclusion_reason,
    to_observation as _to_observation,
    weather_row as _weather_row,
)

SCENE_SQL = """
SELECT land_id, date, sensor, scene_id,
       ndvi_avg, evi_avg, ndre_avg, ndmi_avg,
       cloud_cover, parcel_cloud_cover_pct,
       vv_avg, vh_avg
FROM agric_satellite.parcel_scene_products
WHERE land_id = :land_id
  AND date >= :start
  AND date < :end_exclusive
ORDER BY date, sensor, scene_id
"""


# PostgreSQL SQLSTATE values that distinguish common driver failures.
# These are recorded as-is when present; err_kind is a stable label for logs.
_SQLSTATE_KIND = {
    "42703": "undefined_column",
    "42P01": "undefined_table",
    "42804": "type_mismatch",
    "42883": "type_mismatch",
    "22P02": "type_mismatch",
    "55P03": "lock_timeout",
    "40P01": "lock_timeout",
    "55P04": "lock_timeout",
    "57014": "statement_timeout",
    "42501": "insufficient_privilege",
    "08000": "connection_closed",
    "08003": "connection_closed",
    "08006": "connection_closed",
    "08001": "connection_closed",
    "57P01": "connection_closed",
}

_KIND_FROM_TYPE = {
    "UndefinedColumnError": "undefined_column",
    "UndefinedTableError": "undefined_table",
    "DatatypeMismatchError": "type_mismatch",
    "UndefinedFunctionError": "type_mismatch",
    "InvalidTextRepresentationError": "type_mismatch",
    "LockNotAvailableError": "lock_timeout",
    "DeadlockDetectedError": "lock_timeout",
    "QueryCanceledError": "statement_timeout",
    "InterfaceError": "connection_closed",
    "ConnectionDoesNotExistError": "connection_closed",
    "ConnectionFailureError": "connection_closed",
    "InsufficientPrivilegeError": "insufficient_privilege",
}

_URI_RE = re.compile(
    r"(?i)(?:postgres(?:ql)?(?:\+asyncpg)?|mysql|mongodb|redis|amqp|https?)://\S+"
)
_PASSWORD_RE = re.compile(r"(?i)(password|passwd|pwd|secret|api[_-]?key)\s*[=:]\s*\S+")
_BEARER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*|bearer\s+)\S+")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_SQL_BLOCK_RE = re.compile(
    r"\[SQL:.*?\](?:\s*\[parameters:.*?\])?", re.DOTALL | re.IGNORECASE
)
_PARAMS_BLOCK_RE = re.compile(r"\[parameters:.*?\]", re.DOTALL | re.IGNORECASE)
_SAFE_ORIG_MAX = 240


def _exception_chain(exc: BaseException) -> list[BaseException]:
    rows: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        rows.append(current)
        nxt = getattr(current, "orig", None)
        if not isinstance(nxt, BaseException):
            nxt = current.__cause__ or current.__context__
        current = nxt if isinstance(nxt, BaseException) else None
    return rows


def _sqlstate_of(exc: BaseException) -> str | None:
    for attr in ("sqlstate", "pgcode"):
        raw = getattr(exc, attr, None)
        if raw is None:
            continue
        text_code = str(raw).strip().upper()
        if len(text_code) == 5 and text_code.isalnum():
            return text_code
    diag = getattr(exc, "diag", None)
    if diag is not None:
        raw = getattr(diag, "sqlstate", None) or getattr(diag, "pgcode", None)
        if raw is not None:
            text_code = str(raw).strip().upper()
            if len(text_code) == 5 and text_code.isalnum():
                return text_code
    return None


def safe_error_text(value: str, *, limit: int = _SAFE_ORIG_MAX) -> str:
    """Strip credentials, URIs, SQL parameter blocks, and tokens from an error string."""
    text_value = value or ""
    text_value = _SQL_BLOCK_RE.sub("[SQL:redacted]", text_value)
    text_value = _PARAMS_BLOCK_RE.sub("[parameters:redacted]", text_value)
    text_value = _URI_RE.sub("[uri:redacted]", text_value)
    text_value = _PASSWORD_RE.sub(r"\1=[redacted]", text_value)
    text_value = _BEARER_RE.sub(r"\1[redacted]", text_value)
    text_value = _JWT_RE.sub("[jwt:redacted]", text_value)
    text_value = " ".join(text_value.split())
    if len(text_value) > limit:
        return text_value[: limit - 3] + "..."
    return text_value


def describe_query_error(exc: BaseException) -> dict[str, str | None]:
    """Structured, credential-safe diagnostics for a DB/driver exception chain.

    Prefer the underlying driver's sqlstate and type. Never include statement,
    bound parameters, DSN, or exc_info — SQLAlchemy's string form often embeds
    those. Missing sqlstate/orig is allowed; err_kind falls back to 'other'.
    """
    chain = _exception_chain(exc)
    sqlstate = None
    for item in reversed(chain):
        sqlstate = _sqlstate_of(item)
        if sqlstate:
            break
    driver = chain[-1] if chain else exc
    driver_exc_type = type(driver).__name__
    kind = _SQLSTATE_KIND.get(sqlstate or "")
    if kind is None:
        kind = _KIND_FROM_TYPE.get(driver_exc_type)
    if kind is None:
        kind = _KIND_FROM_TYPE.get(type(exc).__name__)
    if kind is None:
        lowered = " ".join(str(item) for item in chain).lower()
        if "connection is closed" in lowered or "connection was closed" in lowered:
            kind = "connection_closed"
        elif (
            "toordinal" in lowered
            or "expected a datetime.date" in lowered
            or "got 'str'" in lowered
            or 'got "str"' in lowered
        ):
            kind = "type_mismatch"
        else:
            kind = "other"
    # Prefer the innermost orig message (usually "column X does not exist")
    raw_msg = str(driver) if str(driver) else str(exc)
    return {
        "err_type": type(exc).__name__,
        "driver_exc_type": driver_exc_type,
        "sqlstate": sqlstate,
        "err_kind": kind,
        "safe_orig": safe_error_text(raw_msg) or None,
        "exc_chain": ">".join(type(item).__name__ for item in chain),
    }


def _log_query_failed(
    event: str,
    exc: BaseException,
    *,
    land_id: str,
    start: str | None = None,
    end_exclusive: str | None = None,
    end_inclusive: str | None = None,
    job_id: str | None = None,
) -> None:
    fields = describe_query_error(exc)
    payload: dict[str, Any] = {"land_id": str(land_id), **fields}
    if start:
        payload["start"] = str(start)[:10]
    if end_exclusive:
        payload["end_exclusive"] = str(end_exclusive)[:10]
    if end_inclusive:
        payload["end_inclusive"] = str(end_inclusive)[:10]
    if job_id:
        payload["job_id"] = str(job_id)
    # trace_id is already bound on the request logger via contextvars.
    # Do not pass exc_info: the raw traceback includes SQL and parameters.
    logger.warning(event, **payload)








def _as_bind_date(value: Any) -> date:
    """Convert a period window value to datetime.date for asyncpg DATE binds.

    text(SCENE_SQL)/text(WEATHER_SQL) parameters are untyped (NullType). PostgreSQL
    still infers DATE from `date >= $n`. asyncpg then encodes with date_encode,
    which calls value.toordinal(); a str becomes DataError (invalid input for
    query argument). DMS string *literals* succeed because they are not binds.
    datetime is a date subclass, so it is converted via .date() first.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty date")
    return date.fromisoformat(raw[:10])


async def load_scene_observations(
    land_id: str,
    start: str,
    end_exclusive: str,
    *,
    job_id: str | None = None,
) -> list[dict[str, Any]] | None:
    """Return observations, or None if the product table cannot be read."""
    if not (settings.database_url or "").strip():
        return None
    try:
        from app.core.database import async_session

        async with async_session() as db:
            result = await db.execute(
                text(SCENE_SQL),
                {
                    "land_id": str(land_id),
                    "start": _as_bind_date(start),
                    "end_exclusive": _as_bind_date(end_exclusive),
                },
            )
            rows = [dict(row) for row in result.mappings().all()]
        return [_to_observation(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        # Failed read must stay None so callers raise SCENE_QUERY_FAILED,
        # never a silent empty-optics or evidence-limited brief.
        _log_query_failed(
            "biweekly_scene_query_failed",
            exc,
            land_id=land_id,
            start=start,
            end_exclusive=end_exclusive,
            job_id=job_id,
        )
        return None


async def count_period_scenes(
    land_id: str, start: str, end_exclusive: str
) -> dict[str, int] | None:
    rows = await load_scene_observations(land_id, start, end_exclusive)
    if rows is None:
        return None
    optical = [row for row in rows if row["quality_status"] == "eligible_descriptive"]
    radar = [row for row in rows if str(row.get("sensor") or "").upper() == "S1"]
    return {
        "optical_descriptive_days": len({row["date"] for row in optical}),
        "s1_products": len(radar),
        "has_eligible_optics": bool(optical),
    }


def is_maize_crop(crop_type: str | None) -> bool:
    text_value = (crop_type or "").strip().lower()
    return any(token in text_value for token in ("maize", "corn", "玉米"))


def is_wheat_crop(crop_type: str | None) -> bool:
    text_value = (crop_type or "").strip().lower()
    if not text_value:
        return False
    return any(token in text_value for token in ("wheat", "小麦", "冬小麦"))


WEATHER_SQL = """
SELECT date, temperature_2m_min, temperature_2m_max, precipitation_sum, et0_fao_mm
FROM agric_satellite.weather_daily
WHERE land_id = :land_id
  AND date >= :start
  AND date <= :end_inclusive
ORDER BY date
"""

LAND_SQL = """
SELECT land_id, land_name, crop_type
FROM agric_satellite.land_parcels
WHERE land_id = :land_id AND deleted_at IS NULL
"""



def _summarize_weather(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "days": 0,
            "precipitation_mm": None,
            "et0_mm": None,
            "min_c": None,
            "max_c": None,
        }
    rains = [
        row["precipitation_mm"]
        for row in rows
        if row.get("precipitation_mm") is not None
    ]
    et0s = [row["et0_mm"] for row in rows if row.get("et0_mm") is not None]
    mins = [row["min_c"] for row in rows if row.get("min_c") is not None]
    maxs = [row["max_c"] for row in rows if row.get("max_c") is not None]
    return {
        "days": len(rows),
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "precipitation_mm": sum(rains) if rains else None,
        "et0_mm": sum(et0s) if et0s else None,
        "min_c": min(mins) if mins else None,
        "max_c": max(maxs) if maxs else None,
        "missing_precipitation": len(rains) != len(rows),
        "missing_et0": len(et0s) != len(rows),
    }


async def load_weather_series(
    land_id: str, start: str, end_inclusive: str
) -> list[dict[str, Any]] | None:
    """Return daily weather rows, or None if the table cannot be read."""
    if not (settings.database_url or "").strip():
        return None
    try:
        from app.core.database import async_session

        async with async_session() as db:
            result = await db.execute(
                text(WEATHER_SQL),
                {
                    "land_id": str(land_id),
                    "start": _as_bind_date(start),
                    "end_inclusive": _as_bind_date(end_inclusive),
                },
            )
            rows = [dict(row) for row in result.mappings().all()]
        return [_weather_row(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        _log_query_failed(
            "biweekly_weather_query_failed",
            exc,
            land_id=land_id,
            start=start,
            end_inclusive=end_inclusive,
        )
        return None


async def load_land_meta(land_id: str) -> dict[str, Any] | None:
    if not (settings.database_url or "").strip():
        return None
    try:
        from app.core.database import async_session

        async with async_session() as db:
            result = await db.execute(text(LAND_SQL), {"land_id": str(land_id)})
            row = result.mappings().first()
        if not row:
            return {"land_id": str(land_id), "land_name": None, "crop_type": None}
        return {
            "land_id": str(row["land_id"]),
            "land_name": row.get("land_name"),
            "crop_type": row.get("crop_type"),
        }
    except Exception as exc:  # noqa: BLE001
        _log_query_failed(
            "biweekly_land_meta_query_failed",
            exc,
            land_id=land_id,
        )
        return None


def summarize_weather(rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if rows is None:
        return None
    return _summarize_weather(rows)
