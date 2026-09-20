"""下载机天气 API 日配额状态。

天气任务和 HTTP claim agent 运行在不同进程中，使用下载机本地 Redis
共享“当天天气 API 已达到配额”的状态。Redis 仅是本机 Celery broker，
因此这个标记不会影响其他下载机。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

DAILY_API_LIMIT_MESSAGE = (
    "Daily API request limit exceeded. Please try again tomorrow."
)
WEATHER_WORK_TYPE = "weather_backfill"

# 项目现有天气调度按北京时间计算“今天”，这里保持同一口径，避免 UTC 午夜
# 与业务日不一致导致下载机提前恢复领取天气任务。
LOCAL_TIMEZONE = timezone(timedelta(hours=8))
DAILY_LIMIT_KEY_PREFIX = "openfarm:weather:daily-api-limit"

_client_lock = threading.Lock()
_client: Any = None
_client_url = ""
_client_failed_until = 0.0
_RETRY_SECONDS = 30.0


def _redis_url() -> str:
    """读取下载机本地 Redis 地址；未配置时沿用本机 Celery 默认地址。"""
    return (os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0").strip()


def _local_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(LOCAL_TIMEZONE)
    if current.tzinfo is None:
        return current.replace(tzinfo=LOCAL_TIMEZONE)
    return current.astimezone(LOCAL_TIMEZONE)


def daily_limit_key(now: datetime | None = None) -> str:
    """返回指定业务日的天气 API 配额标记 key。"""
    return f"{DAILY_LIMIT_KEY_PREFIX}:{_local_now(now).date().isoformat()}"


def _seconds_until_next_day(now: datetime | None = None) -> int:
    current = _local_now(now)
    next_day = datetime.combine(
        current.date() + timedelta(days=1),
        datetime_time.min,
        tzinfo=LOCAL_TIMEZONE,
    )
    return max(1, int((next_day - current).total_seconds()))


def _get_client() -> Any | None:
    """懒加载并缓存本机 Redis 客户端；Redis 故障时允许业务降级运行。"""
    global _client, _client_url, _client_failed_until
    url = _redis_url()
    if _client is not None and _client_url == url:
        return _client
    if _client_url == url and time.monotonic() < _client_failed_until:
        return None

    with _client_lock:
        if _client is not None and _client_url == url:
            return _client
        if _client_url == url and time.monotonic() < _client_failed_until:
            return None
        try:
            import redis

            client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )
            client.ping()
            _client = client
            _client_url = url
            _client_failed_until = 0.0
            return client
        except Exception as exc:
            _client = None
            _client_url = url
            _client_failed_until = time.monotonic() + _RETRY_SECONDS
            logger.warning("weather_daily_limit_redis_unavailable: %s", exc)
            return None


def _connection_failed(client: Any, exc: Exception) -> None:
    global _client, _client_failed_until
    with _client_lock:
        if _client is client:
            _client = None
            _client_failed_until = time.monotonic() + _RETRY_SECONDS
    logger.warning("weather_daily_limit_redis_operation_failed: %s", exc)


def reset_client_for_tests() -> None:
    """清除 Redis 客户端缓存，供单元测试隔离使用。"""
    global _client, _client_url, _client_failed_until
    with _client_lock:
        _client = None
        _client_url = ""
        _client_failed_until = 0.0


def weather_daily_limit_reached(now: datetime | None = None) -> bool:
    """判断下载机在当前业务日是否已达到天气 API 日配额。"""
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(client.exists(daily_limit_key(now)))
    except Exception as exc:
        _connection_failed(client, exc)
        return False


def mark_weather_daily_limit_reached(now: datetime | None = None) -> bool:
    """记录天气 API 当日配额已耗尽，并在北京时间次日自动过期。"""
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(
            client.set(
                daily_limit_key(now),
                "1",
                ex=_seconds_until_next_day(now),
            )
        )
    except Exception as exc:
        _connection_failed(client, exc)
        return False


__all__ = [
    "DAILY_API_LIMIT_MESSAGE",
    "WEATHER_WORK_TYPE",
    "daily_limit_key",
    "mark_weather_daily_limit_reached",
    "reset_client_for_tests",
    "weather_daily_limit_reached",
]
