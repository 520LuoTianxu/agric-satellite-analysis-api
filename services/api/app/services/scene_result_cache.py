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
SCENE_RESULT_DELAYED = "openfarm:satellite:scene-result:delayed"
SCENE_RESULT_DEAD_LETTER = "openfarm:satellite:scene-result:dead-letter"
SCENE_RESULT_ITEM_PREFIX = "openfarm:satellite:scene-result:item:"
SCENE_RESULT_RETRY_PREFIX = "openfarm:satellite:scene-result:retry:"
SCENE_RESULT_DEAD_LETTER_REASON_PREFIX = (
    "openfarm:satellite:scene-result:dead-letter-reason:"
)
SCENE_RESULT_TTL_SECONDS = 24 * 60 * 60
SCENE_RESULT_RETRY_BASE_SECONDS = 2
SCENE_RESULT_RETRY_MAX_SECONDS = 300
SCENE_RESULT_PROMOTE_BATCH_SIZE = 50
SCENE_RESULT_DEAD_LETTER_MAX_ITEMS = 1000

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

# 出队恢复、确认、延迟重试都使用原子脚本，避免列表间移动时进程退出造成回执丢失。
_RECOVER_PROCESSING_SCRIPT = """
if redis.call('EXISTS', ARGV[2]) == 1 then
    -- 先建立待处理副本，再移除processing；命令中途失败时宁可重复也不能丢任务。
    redis.call('LREM', KEYS[2], 0, ARGV[1])
    redis.call('LPUSH', KEYS[2], ARGV[1])
    redis.call('EXPIRE', KEYS[2], ARGV[3])
    return redis.call('LREM', KEYS[1], 1, ARGV[1])
end
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed > 0 then
    redis.call('DEL', ARGV[4])
    redis.call('ZREM', KEYS[3], ARGV[1])
end
return removed
"""

_PROMOTE_DUE_RETRIES_SCRIPT = """
-- 使用Redis服务器时间统一判断到期点，避免多台API主机时钟偏差造成提前重试。
local now = redis.call('TIME')
local now_seconds = tonumber(now[1]) + tonumber(now[2]) / 1000000
local due_ids = redis.call(
    'ZRANGEBYSCORE', KEYS[1], '-inf', now_seconds, 'LIMIT', 0, ARGV[1]
)
for _, result_id in ipairs(due_ids) do
    if redis.call('EXISTS', ARGV[2] .. result_id) == 1 then
        -- 先回到ready队列再从延迟集合删除，脚本中断时允许幂等重复投递。
        redis.call('LREM', KEYS[2], 0, result_id)
        redis.call('LPUSH', KEYS[2], result_id)
        redis.call('EXPIRE', KEYS[2], ARGV[3])
    else
        redis.call('DEL', ARGV[4] .. result_id)
    end
    redis.call('ZREM', KEYS[1], result_id)
end
return #due_ids
"""

_SCHEDULE_RETRY_SCRIPT = """
if redis.call('EXISTS', KEYS[4]) == 0 then
    -- 原始回执已过期时清理残留队列索引，避免无效ID长期留在processing或延迟集合。
    local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
    if removed > 0 then
        redis.call('LREM', KEYS[2], 0, ARGV[1])
    end
    redis.call('DEL', KEYS[3])
    redis.call('ZREM', KEYS[5], ARGV[1])
    return {removed, 0, 0}
end
-- 延迟记录或死信原因已存在，表示此前已结算；响应丢失后重跑不能再次增加重试次数。
if redis.call('EXISTS', KEYS[6]) == 1
    or redis.call('ZSCORE', KEYS[5], ARGV[1]) then
    local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
    if removed > 0 then
        redis.call('LREM', KEYS[2], 0, ARGV[1])
    end
    return {0, 0, 0}
end
local attempt = redis.call('INCR', KEYS[3])
redis.call('EXPIRE', KEYS[3], ARGV[2])
local delay = tonumber(ARGV[3])
for i = 2, tonumber(attempt) do
    if delay >= tonumber(ARGV[4]) then
        break
    end
    delay = math.min(delay * 2, tonumber(ARGV[4]))
end
local now = redis.call('TIME')
local now_seconds = tonumber(now[1]) + tonumber(now[2]) / 1000000
-- 按2倍指数退避并封顶；Redis脚本将延迟记录与processing移出操作原子提交。
redis.call('ZADD', KEYS[5], now_seconds + delay, ARGV[1])
redis.call('EXPIRE', KEYS[5], ARGV[2])
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed == 0 then
    redis.call('LREM', KEYS[2], 0, ARGV[1])
    return {0, 0, 0}
end
redis.call('LREM', KEYS[2], 0, ARGV[1])
return {removed, attempt, delay}
"""

_ACK_RESULT_SCRIPT = """
-- 只在processing中实际移除本回执时清理缓存和各索引，重复确认保持幂等。
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed > 0 then
    redis.call('LREM', KEYS[2], 0, ARGV[1])
    redis.call('LREM', KEYS[6], 0, ARGV[1])
    redis.call('DEL', KEYS[3], KEYS[4])
    redis.call('DEL', KEYS[7])
    redis.call('ZREM', KEYS[5], ARGV[1])
end
return removed
"""

_DEAD_LETTER_RESULT_SCRIPT = """
if redis.call('EXISTS', KEYS[4]) == 1 then
    -- 保留一份有界死信凭证后再确认processing，命令异常时由后续重试完成结算。
    redis.call('LREM', KEYS[3], 0, ARGV[1])
    redis.call('LPUSH', KEYS[3], ARGV[1])
    redis.call('LTRIM', KEYS[3], 0, tonumber(ARGV[4]) - 1)
    redis.call('EXPIRE', KEYS[3], ARGV[3])
    redis.call('SET', KEYS[5], ARGV[2], 'EX', ARGV[3])
end
-- 先持久化死信，再清除processing、重试计数和延迟索引，防止坏回执被重新当作新任务消费。
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed > 0 then
    redis.call('LREM', KEYS[2], 0, ARGV[1])
    redis.call('DEL', KEYS[6])
    redis.call('ZREM', KEYS[7], ARGV[1])
end
return removed
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


async def enqueue_scene_result(
    envelope: dict[str, Any], redis_client: Any | None = None
) -> dict[str, Any]:
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
    owns_redis_client = redis_client is None
    if redis_client is None:
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
        if owns_redis_client:
            await redis_client.aclose()
    return {
        "result_id": result_id,
        "queued": bool(created),
        "ttl_seconds": SCENE_RESULT_TTL_SECONDS,
    }


async def _recover_processing(redis_client: Any) -> None:
    """API 重启时把上次处理中但仍未过期的缓存重新放回待处理队列。"""
    processing = await redis_client.lrange(SCENE_RESULT_PROCESSING, 0, -1)
    for result_id in set(processing):
        await redis_client.eval(
            _RECOVER_PROCESSING_SCRIPT,
            3,
            SCENE_RESULT_PROCESSING,
            SCENE_RESULT_QUEUE,
            SCENE_RESULT_DELAYED,
            result_id,
            _item_key(result_id),
            str(SCENE_RESULT_TTL_SECONDS),
            f"{SCENE_RESULT_RETRY_PREFIX}{result_id}",
        )


async def _promote_due_retries(redis_client: Any) -> None:
    """把到达退避时间的任务原子移回队列，避免单条失败阻塞其他场景。"""
    await redis_client.eval(
        _PROMOTE_DUE_RETRIES_SCRIPT,
        2,
        SCENE_RESULT_DELAYED,
        SCENE_RESULT_QUEUE,
        str(SCENE_RESULT_PROMOTE_BATCH_SIZE),
        SCENE_RESULT_ITEM_PREFIX,
        str(SCENE_RESULT_TTL_SECONDS),
        SCENE_RESULT_RETRY_PREFIX,
    )


async def _schedule_retry(redis_client: Any, result_id: str) -> tuple[int, int] | None:
    """失败时按2倍退避并保留processing凭证，API重启后仍能恢复该回执。"""
    result = await redis_client.eval(
        _SCHEDULE_RETRY_SCRIPT,
        6,
        SCENE_RESULT_PROCESSING,
        SCENE_RESULT_QUEUE,
        f"{SCENE_RESULT_RETRY_PREFIX}{result_id}",
        _item_key(result_id),
        SCENE_RESULT_DELAYED,
        f"{SCENE_RESULT_DEAD_LETTER_REASON_PREFIX}{result_id}",
        result_id,
        str(SCENE_RESULT_TTL_SECONDS),
        str(SCENE_RESULT_RETRY_BASE_SECONDS),
        str(SCENE_RESULT_RETRY_MAX_SECONDS),
    )
    if not result or int(result[0]) == 0:
        return None
    return int(result[1]), int(result[2])


async def _ack_result(redis_client: Any, result_id: str) -> None:
    """场景落库成功后原子删除缓存，崩溃前后都保持至少一次投递。"""
    await redis_client.eval(
        _ACK_RESULT_SCRIPT,
        7,
        SCENE_RESULT_PROCESSING,
        SCENE_RESULT_QUEUE,
        _item_key(result_id),
        f"{SCENE_RESULT_RETRY_PREFIX}{result_id}",
        SCENE_RESULT_DELAYED,
        SCENE_RESULT_DEAD_LETTER,
        f"{SCENE_RESULT_DEAD_LETTER_REASON_PREFIX}{result_id}",
        result_id,
    )


async def _dead_letter_result(
    redis_client: Any,
    result_id: str,
    reasons: dict[str, str],
) -> None:
    """确定性坏回执停止重试，保留原内容和原因供24小时内排查。"""
    await redis_client.eval(
        _DEAD_LETTER_RESULT_SCRIPT,
        7,
        SCENE_RESULT_PROCESSING,
        SCENE_RESULT_QUEUE,
        SCENE_RESULT_DEAD_LETTER,
        _item_key(result_id),
        f"{SCENE_RESULT_DEAD_LETTER_REASON_PREFIX}{result_id}",
        f"{SCENE_RESULT_RETRY_PREFIX}{result_id}",
        SCENE_RESULT_DELAYED,
        result_id,
        json.dumps(reasons, ensure_ascii=False, sort_keys=True),
        str(SCENE_RESULT_TTL_SECONDS),
        str(SCENE_RESULT_DEAD_LETTER_MAX_ITEMS),
    )


def _scene_upsert_succeeded(stats: dict[str, Any]) -> bool:
    """兼容直接入库和OSS标签入库两种结构，以实际场景写入行数判定成功。"""
    if int(stats.get("scene_upserts") or 0) > 0:
        return True
    oss_stats = stats.get("oss")
    return isinstance(oss_stats, dict) and int(oss_stats.get("scene_upserts") or 0) > 0


def _scene_result_failures(stats: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """区分可重试故障与确定性坏回执，避免两类问题采用相同队列策略。"""
    oss_stats = stats.get("oss")
    if not isinstance(oss_stats, dict):
        return [], {}
    retryable = oss_stats.get("retryable_failures")
    permanent = oss_stats.get("permanent_rejections")
    retryable_labels = (
        [str(label) for label in retryable] if isinstance(retryable, list) else []
    )
    permanent_reasons = (
        {str(label): str(reason) for label, reason in permanent.items()}
        if isinstance(permanent, dict)
        else {}
    )
    return retryable_labels, permanent_reasons


async def consume_cached_scene_results(redis_client: Any | None = None) -> None:
    """API 进程后台消费 Redis，并在 API 机执行 OSS 下载及 PostgreSQL 入库。"""
    owns_redis_client = redis_client is None
    if redis_client is None:
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
                await _promote_due_retries(redis_client)
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
                    await _ack_result(redis_client, result_id)
                    continue
                envelope = json.loads(raw)
                from agric_satellite_analysis_common.result_apply import (
                    apply_result_envelope,
                )

                # 结果应用包含同步数据库和 OSS 网络操作，放到线程避免阻塞 API 事件循环。
                stats = await asyncio.to_thread(apply_result_envelope, envelope)
                if not isinstance(stats, dict):
                    raise RuntimeError(f"scene result was not applied: {stats}")
                retryable_failures, permanent_rejections = _scene_result_failures(stats)
                if retryable_failures:
                    # 有一部分成功也要重试；场景唯一键保证已成功部分重复upsert幂等。
                    raise RuntimeError(
                        f"scene result has retryable failures: {retryable_failures}"
                    )
                scene_applied = _scene_upsert_succeeded(stats)
                if not scene_applied and not permanent_rejections:
                    # 该队列只收场景JSON；无场景产品的回执不应占用重试队列。
                    permanent_rejections = {"result": "no_scene_product"}

                if scene_applied:
                    # 预警重算必须在场景事务完成后执行，否则最新观测可能还未进入主库。
                    from app.services.agri_alerts import (
                        evaluate_alerts_for_scene_result,
                    )

                    try:
                        await asyncio.to_thread(
                            evaluate_alerts_for_scene_result,
                            envelope,
                            stats,
                        )
                    except Exception as alert_exc:
                        logger.exception(
                            "satellite_scene_alert_evaluation_failed",
                            result_id=result_id,
                            error=str(alert_exc),
                        )
                    # 收获占比只入队不计算，outbox 后台合并重算，不拖慢结果消费。
                    from app.services.harvest_progress import (
                        enqueue_from_scene_result,
                    )

                    try:
                        await enqueue_from_scene_result(envelope, stats)
                    except Exception as harvest_exc:
                        logger.exception(
                            "harvest_progress_enqueue_failed",
                            result_id=result_id,
                            error=str(harvest_exc),
                        )

                if permanent_rejections:
                    await _dead_letter_result(
                        redis_client,
                        result_id,
                        permanent_rejections,
                    )
                    logger.error(
                        "satellite_scene_result_rejected",
                        result_id=result_id,
                        reasons=permanent_rejections,
                        scene_upserts=int(stats.get("scene_upserts") or 0),
                    )
                else:
                    await _ack_result(redis_client, result_id)
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
                    while True:
                        try:
                            scheduled = await _schedule_retry(redis_client, result_id)
                            if scheduled:
                                attempt, delay_seconds = scheduled
                                logger.warning(
                                    "satellite_scene_result_retry_scheduled",
                                    result_id=result_id,
                                    attempt=attempt,
                                    delay_seconds=delay_seconds,
                                )
                            # Lua脚本原子移动 processing→delayed；响应丢失时重复执行会安全返回未移动。
                            break
                        except Exception:
                            logger.exception(
                                "satellite_scene_result_retry_schedule_failed",
                                result_id=result_id,
                            )
                            await asyncio.sleep(2)
                else:
                    # Redis本身不可用时没有任务ID可记录退避，短暂等待避免错误热循环。
                    await asyncio.sleep(2)
    finally:
        if owns_redis_client:
            await redis_client.aclose()
