"""影像拍摄时间（STAC properties.datetime）的统一规范化。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = ["normalize_acquired_at", "stac_item_acquired_at"]


def normalize_acquired_at(value: Any) -> str | None:
    """把 datetime / ISO 字符串规范为带时区的 UTC ISO8601；无法解析返回 None。

    无时区的值按 STAC 约定视为 UTC。只有日期、没有时分秒的字符串不算拍摄时间，返回 None，
    避免把 00:00:00 伪装成真实拍摄时刻。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if len(raw) <= 10 or "T" not in raw.upper() and " " not in raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def stac_item_acquired_at(item: Any) -> str | None:
    """从 pystac Item 或 STAC dict 取拍摄时间：datetime 优先，其次 start_datetime。"""
    if item is None:
        return None
    if isinstance(item, dict):
        props = item.get("properties") or {}
        dt = None
    else:
        props = getattr(item, "properties", None) or {}
        dt = getattr(item, "datetime", None)
    return normalize_acquired_at(dt) or normalize_acquired_at(
        props.get("datetime") or props.get("start_datetime")
    )
