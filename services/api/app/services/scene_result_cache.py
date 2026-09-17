"""Redis 缓存队列：缓冲下载机回调，再由 API 侧异步落库。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import logger

SCENE_RESULT_QUEUE = "openfarm:satellite:scene-result:queue"
SCENE_RESULT_PROCESSING = "openfarm:satellite:scene-result:processing"
SCENE_RESULT_ITEM_PREFIX = "openfarm:satellite:scene-result:item:"
SCENE_RESULT_TTL_SECONDS = 24 * 60 * 60

# SET、LPUSH 和队列 TTL 必须原子执行，避免 API 在写入缓存后进程崩溃而丢失队列通知。
_ENQUEUE_SCRIPT = """
local created = redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2], 'NX')
if created then
    redis.call('LPUSH', KEYS[2], ARGV[3])
    redis.call('EXPIRE', KEYS[2], ARGV[2])
    return 1
end
return 0
"""


def _item_key(result_id: str) -> str:
    return f"{SCENE_RESULT_ITEM_PREFIX}{result_id}"


def _result_id(envelope: dict[str, Any]) -> str:
    """构造稳定幂等键，重复 HTTP 重试只进入一次 Redis 队列。"""
    extras = envelope.get("extras")
    if isinstance(extras, dict):
        identity = {
            key: extras.get(key)
            for key in ("land_id", "date", "sensor", "scene_id", "json_oss_key")
            if extras.get(key) is not None
        }
    else:
        identity = {}
    if not identity:
        identity = envelope
    raw = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def enqueue_scene_result(envelope: dict[str, Any]) -> dict[str, Any]:
    """将待入库 OSS 回调放入 Redis，缓存项最多保留 1 天。"""
    if not isinstance(envelope, dict):
        raise ValueError("scene result envelope must be an object")
    oss_urls = envelope.get("oss_urls")
    if not isinstance(oss_urls, dict) or not oss_urls:
        raise ValueError("scene result cache requires non-empty oss_urls")

    result_id = _result_id(envelope)
    encoded = json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    redis_client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=5.0,
    )
    try:
        created = await redis_client.eval(
            _ENQUEUE_SCRIPT,
            2,
            _item_key(result_id),
            SCENE_RESULT_QUEUE,
            encoded,
            str(SCENE_RESULT_TTL_SECONDS),
            result_id,
        )
    finally:
        await redis_client.aclose()
    return {
        "result_id": result_id,
        "queued": bool(created),
        "ttl_seconds": SCENE_RESULT_TTL_SECONDS,
    }


async def _recover_processing(redis_client: Any) -> None:
    """API 重启时把上次处理中但仍未过期的缓存重新放回待处理队列。"""
    processing = await redis_client.lrange(SCENE_RESULT_PROCESSING, 0, -1)
    for result_id in processing:
        await redis_client.lrem(SCENE_RESULT_PROCESSING, 1, result_id)
        if await redis_client.exists(_item_key(result_id)):
            await redis_client.lpush(SCENE_RESULT_QUEUE, result_id)


def _scene_upsert_succeeded(stats: dict[str, Any]) -> bool:
    if int(stats.get("scene_upserts") or 0) > 0:
        return True
    oss_stats = stats.get("oss")
    return isinstance(oss_stats, dict) and int(oss_stats.get("scene_upserts") or 0) > 0


async def consume_cached_scene_results() -> None:
    """API 进程后台消费 Redis，并在 API 机执行 OSS 下载及 PostgreSQL 入库。"""
    redis_client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=10.0,
    )
    try:
        while True:
            try:
                await _recover_processing(redis_client)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "satellite_scene_result_redis_unavailable",
                    error=str(exc),
                )
                await asyncio.sleep(5)
        while True:
            result_id: str | None = None
            try:
                # BRPOPLPUSH 先转入 processing，API 进程异常退出后可在下次启动恢复。
                result_id = await redis_client.brpoplpush(
                    SCENE_RESULT_QUEUE,
                    SCENE_RESULT_PROCESSING,
                    timeout=5,
                )
                if not result_id:
                    continue
                raw = await redis_client.get(_item_key(result_id))
                if raw is None:
                    await redis_client.lrem(SCENE_RESULT_PROCESSING, 1, result_id)
                    continue
                envelope = json.loads(raw)
                from agric_satellite_analysis_common.result_apply import (
                    apply_result_envelope,
                )

                # 结果应用包含同步数据库和 OSS 网络操作，放到线程避免阻塞 API 事件循环。
                stats = await asyncio.to_thread(apply_result_envelope, envelope)
                if not isinstance(stats, dict) or not _scene_upsert_succeeded(stats):
                    raise RuntimeError(f"scene result was not applied: {stats}")
                await redis_client.lrem(SCENE_RESULT_PROCESSING, 1, result_id)
                await redis_client.delete(_item_key(result_id))
                logger.info(
                    "satellite_scene_result_applied",
                    result_id=result_id,
                    stats=stats,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "satellite_scene_result_consume_failed",
                    result_id=result_id,
                    error=str(exc),
                )
                if result_id:
                    try:
                        await redis_client.lrem(SCENE_RESULT_PROCESSING, 1, result_id)
                        if await redis_client.exists(_item_key(result_id)):
                            await redis_client.lpush(SCENE_RESULT_QUEUE, result_id)
                    except Exception:
                        logger.exception(
                            "satellite_scene_result_requeue_failed",
                            result_id=result_id,
                        )
                await asyncio.sleep(2)
    finally:
        await redis_client.aclose()
