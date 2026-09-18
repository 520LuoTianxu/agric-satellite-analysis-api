"""按 Celery task name 将历史 ingest 队列拆到资源隔离队列。

默认只统计不修改 Redis；生产迁移必须显式传 ``--apply``，并先暂停旧 worker。
脚本使用中间 list 搬运单条消息，进程中断时消息仍留在中间 list，不会静默丢失。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from collections import Counter
from typing import Any

from agric_satellite_analysis_common.celery_app import (
    CPU_COMPUTE_QUEUE,
    LEGACY_INGEST_QUEUE,
    SATELLITE_DOWNLOAD_QUEUE,
)

try:
    import redis
except ImportError as exc:  # pragma: no cover - download image includes redis
    raise SystemExit("redis package is required") from exc


STAGING_QUEUE = "celery:repartition:ingest"
SATELLITE_PREFIXES = (
    "app.tasks.agri_lonlat.",
    "app.tasks.sentinel1.",
    "app.tasks.satellite_batch.",
)
CPU_PREFIXES = (
    "app.tasks.backfill.",
    "app.tasks.weather.",
    "app.tasks.soil.",
    "app.tasks.pipeline.",
    "app.tasks.vegetation.",
    "app.tasks.ndvi.",
    "app.tasks.indices.",
    "app.tasks.agri_bridge.",
    "app.tasks.bridge_stac_cogs_to_agri_lonlat.",
    "app.tasks.agri_alerts.",
    "app.tasks.assessment_report.",
    "app.tasks.season_growth_report.",
    "app.tasks.overview_preagg.",
)


def _decode_body(value: Any) -> dict[str, Any] | None:
    """兼容 Celery Redis envelope 中可能出现的 base64 JSON body。"""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    candidates = [value]
    try:
        candidates.append(base64.b64decode(value).decode("utf-8"))
    except Exception:
        pass
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def task_name(raw: bytes | str) -> str | None:
    """读取 Celery 消息 header 中的任务名，无法解析时返回 None。"""
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    headers = envelope.get("headers")
    if isinstance(headers, dict) and headers.get("task"):
        return str(headers["task"])
    # 兼容极老消息把 headers 放进序列化 body 的情况。
    body = _decode_body(envelope.get("body"))
    if isinstance(body, dict) and body.get("task"):
        return str(body["task"])
    return None


def destination(task: str | None) -> str | None:
    """返回目标队列；未识别任务保留在旧 ingest 队列。"""
    if task and task.startswith(SATELLITE_PREFIXES):
        return SATELLITE_DOWNLOAD_QUEUE
    if task and task.startswith(CPU_PREFIXES):
        return CPU_COMPUTE_QUEUE
    return None


def _summary(client: Any, source: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for raw in client.lrange(source, 0, -1):
        task = task_name(raw)
        counts[destination(task) or "unclassified"] += 1
    return counts


def repartition(client: Any) -> Counter[str]:
    """迁移一批启动时已有消息，避免与迁移过程中新增消息互相影响。"""
    counts: Counter[str] = Counter()
    initial_count = int(client.llen(LEGACY_INGEST_QUEUE))
    for _ in range(initial_count):
        raw = client.rpoplpush(LEGACY_INGEST_QUEUE, STAGING_QUEUE)
        if raw is None:
            break
        task = task_name(raw)
        target = destination(task)
        if target is None:
            # 未分类消息回到旧队列，后续仍可由兼容 worker 处理。
            client.rpush(LEGACY_INGEST_QUEUE, raw)
            counts["unclassified"] += 1
        else:
            client.rpush(target, raw)
            counts[target] += 1
        client.lrem(STAGING_QUEUE, 1, raw)
    return counts


def recover_staging(client: Any) -> int:
    """恢复上次中断时的中间消息到 ingest，确保可以安全重试迁移。"""
    recovered = 0
    while True:
        raw = client.rpop(STAGING_QUEUE)
        if raw is None:
            return recovered
        client.rpush(LEGACY_INGEST_QUEUE, raw)
        recovered += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--redis-url",
        default=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
        help="download machine local Celery Redis URL",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually move messages; without it only inspect queue contents",
    )
    parser.add_argument(
        "--recover-staging",
        action="store_true",
        help="put messages left in the staging list back into ingest",
    )
    args = parser.parse_args()
    client = redis.Redis.from_url(args.redis_url)
    try:
        if args.recover_staging:
            print(f"recovered={recover_staging(client)}")
        if args.apply:
            counts = repartition(client)
        else:
            counts = _summary(client, LEGACY_INGEST_QUEUE)
            print("dry-run: Redis unchanged; use --apply after pausing workers")
        for name in (SATELLITE_DOWNLOAD_QUEUE, CPU_COMPUTE_QUEUE, "unclassified"):
            print(f"{name}={counts.get(name, 0)}")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
