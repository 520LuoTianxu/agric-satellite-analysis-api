"""CloudAMQP (RabbitMQ via pika) helpers for the outer task bus.

Never log the full CLOUDAMQP_URL (contains password). Prefer
``connection_label()`` for diagnostics.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

import pika
from pika.adapters.blocking_connection import BlockingChannel, BlockingConnection

from openfarm_common.mq_schemas import ResultMessage, TaskMessage
from openfarm_common.settings import settings

logger = logging.getLogger(__name__)

MAX_REQUEUE = 3


def connection_label(url: str | None = None) -> str:
    """Safe connection summary without password."""
    raw = url or settings.cloudamqp_url
    if not raw:
        return "(unset)"
    try:
        parsed = urlparse(raw)
        host = parsed.hostname or "?"
        vhost = (parsed.path or "/").lstrip("/") or "/"
        user = parsed.username or "?"
        return f"{user}@{host}/{vhost}"
    except Exception:
        return "(invalid-url)"


def _params(url: str | None = None) -> pika.URLParameters:
    raw = url or settings.cloudamqp_url
    if not raw:
        raise RuntimeError("CLOUDAMQP_URL is not set")
    params = pika.URLParameters(raw)
    params.heartbeat = 60
    params.blocked_connection_timeout = 300
    return params


# CloudAMQP plan caps concurrent connections (often 20 for the whole vhost).
# Scene-parallel ingest used thread-local conns (8 workers × 2 celery children
# ≈ 16) and still blew the cap when S1/optical overlapped with mq_consumer.
# Publish hot path now shares ONE process-wide connection under a lock.
_publish_lock = threading.RLock()
_shared: dict[str, Any] = {
    "conn": None,
    "conn_key": None,
    "process_channel": None,
    "download_channel": None,
    "process_queue": None,
    "download_queue": None,
}

# Keep a thread-local only for mq_connection(reuse=True) legacy callers.
_tls = threading.local()


def _publish_max_attempts() -> int:
    try:
        return max(1, int(os.environ.get("CLOUDAMQP_PUBLISH_MAX_ATTEMPTS", "5")))
    except ValueError:
        return 5


def _is_connection_limit_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "connection limit" in text or "not_allowed" in text


def _is_retryable_publish_error(exc: BaseException) -> bool:
    """True for transient AMQP/publish failures worth reconnect+retry."""
    if _is_connection_limit_error(exc):
        return True
    if isinstance(exc, (BrokenPipeError, ConnectionError, TimeoutError)):
        return True
    text = str(exc).lower()
    needles = (
        "connection",
        "broken pipe",
        "stream connection lost",
        "streamlost",
        "eof",
        "connection reset",
        "socket closed",
        "transport",
    )
    return any(n in text for n in needles)


def _close_quiet(obj: Any) -> None:
    if obj is None:
        return
    try:
        if getattr(obj, "is_open", False):
            obj.close()
    except Exception:
        pass


def _invalidate_shared_publisher() -> None:
    """Drop process-wide publish conn/channels (caller must hold _publish_lock)."""
    _close_quiet(_shared.get("process_channel"))
    _close_quiet(_shared.get("download_channel"))
    _close_quiet(_shared.get("conn"))
    _shared["process_channel"] = None
    _shared["download_channel"] = None
    _shared["conn"] = None
    _shared["conn_key"] = None
    _shared["process_queue"] = None
    _shared["download_queue"] = None


def _reset_shared_publisher_for_tests() -> None:
    with _publish_lock:
        _invalidate_shared_publisher()


def _shared_connection(url: str | None = None) -> BlockingConnection:
    """Return the process-wide publish connection (caller holds _publish_lock)."""
    key = url or ""
    conn = _shared.get("conn")
    if conn is not None and _shared.get("conn_key") == key and getattr(conn, "is_open", False):
        return conn
    _invalidate_shared_publisher()
    conn = pika.BlockingConnection(_params(url))
    _shared["conn"] = conn
    _shared["conn_key"] = key
    return conn


def _cached_connection(url: str | None = None) -> BlockingConnection:
    """Thread-local cache for mq_connection(reuse=True) non-publish callers."""
    key = url or ""
    conn = getattr(_tls, "conn", None)
    conn_key = getattr(_tls, "conn_key", None)
    if conn is not None and conn_key == key and getattr(conn, "is_open", False):
        return conn
    if conn is not None:
        _close_quiet(conn)
    conn = pika.BlockingConnection(_params(url))
    _tls.conn = conn
    _tls.conn_key = key
    return conn


@contextmanager
def mq_connection(url: str | None = None, *, reuse: bool = False):
    """Yield a BlockingConnection.

    ``reuse=True`` keeps a thread-local connection open.
    Prefer ``publish_result`` / ``publish_task`` for the hot path — those use
    a process-wide shared connection so scene threads cannot exhaust the
    CloudAMQP connection cap.
    Default still opens+closes for one-shot callers (e.g. consume_forever).
    """
    if reuse:
        yield _cached_connection(url)
        return
    conn = pika.BlockingConnection(_params(url))
    try:
        yield conn
    finally:
        _close_quiet(conn)


def declare_queues(
    channel: BlockingChannel,
    *,
    download_queue: str | None = None,
    process_queue: str | None = None,
    task_queue: str | None = None,
    result_queue: str | None = None,
) -> tuple[str, str]:
    """Declare durable download + process queues. Returns (download_q, process_q).

    ``task_queue`` / ``result_queue`` are one-release aliases for download/process.
    """
    dq = download_queue or task_queue or settings.cloudamqp_download_queue
    pq = process_queue or result_queue or settings.cloudamqp_process_queue
    channel.queue_declare(queue=dq, durable=True)
    channel.queue_declare(queue=pq, durable=True)
    return dq, pq


def publish_json(
    channel: BlockingChannel,
    queue: str,
    payload: dict[str, Any],
    *,
    persistent: bool = True,
) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    channel.basic_publish(
        exchange="",
        routing_key=queue,
        body=body,
        properties=pika.BasicProperties(
            delivery_mode=2 if persistent else 1,
            content_type="application/json",
        ),
    )


def _publish_locked(
    *,
    kind: str,
    payload: dict[str, Any],
    url: str | None,
    queue: str | None,
) -> str:
    """Publish JSON on the process-wide connection. Returns target queue name."""
    attempts = _publish_max_attempts()
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        with _publish_lock:
            try:
                conn = _shared_connection(url)
                # Flush heartbeats / pending frames so a dead shared conn fails
                # before basic_publish (best-effort; invalidate+retry on error).
                try:
                    conn.process_data_events(time_limit=0)
                except Exception:
                    _invalidate_shared_publisher()
                    conn = _shared_connection(url)
                if kind == "process":
                    ch = _shared.get("process_channel")
                    if ch is None or not getattr(ch, "is_open", False):
                        ch = conn.channel()
                        _, rq = declare_queues(ch)
                        _shared["process_queue"] = rq
                        _shared["process_channel"] = ch
                    target = (
                        queue
                        or _shared.get("process_queue")
                        or settings.cloudamqp_process_queue
                    )
                else:
                    ch = _shared.get("download_channel")
                    if ch is None or not getattr(ch, "is_open", False):
                        ch = conn.channel()
                        tq, _ = declare_queues(ch)
                        _shared["download_queue"] = tq
                        _shared["download_channel"] = ch
                    target = (
                        queue
                        or _shared.get("download_queue")
                        or settings.cloudamqp_download_queue
                    )
                publish_json(ch, target, payload)
                return target
            except Exception as exc:
                last_exc = exc
                _invalidate_shared_publisher()
                retryable = _is_retryable_publish_error(exc)
                if attempt < attempts and retryable:
                    logger.warning(
                        "mq_publish_retry kind=%s attempt=%s/%s err=%s",
                        kind,
                        attempt,
                        attempts,
                        exc,
                    )
                else:
                    raise
        # Backoff outside the lock so other publishers can proceed after reconnect.
        time.sleep(min(2.0, 0.2 * (2 ** (attempt - 1))))
    assert last_exc is not None
    raise last_exc


def publish_task(
    message: TaskMessage | dict[str, Any],
    *,
    url: str | None = None,
    queue: str | None = None,
) -> None:
    """Publish a TaskMessage to the download queue (producer helper)."""
    if isinstance(message, dict):
        message = TaskMessage.model_validate(message)
    payload = message.model_dump(mode="json")
    target = _publish_locked(kind="download", payload=payload, url=url, queue=queue)
    logger.info(
        "mq_task_published task_id=%s type=%s queue=%s broker=%s",
        message.task_id,
        message.type,
        target,
        connection_label(url),
    )


def publish_result(
    message: ResultMessage | dict[str, Any],
    *,
    url: str | None = None,
    queue: str | None = None,
) -> None:
    """Publish a ResultMessage to the process queue.

    Uses one process-wide AMQP connection serialized by a lock so scene-parallel
    threads cannot open one TLS connection each (CloudAMQP connection cap).
    """
    if isinstance(message, dict):
        message = ResultMessage.model_validate(message)
    payload = message.model_dump(mode="json")
    target = _publish_locked(kind="process", payload=payload, url=url, queue=queue)
    logger.info(
        "mq_result_published task_id=%s status=%s queue=%s broker=%s",
        message.task_id,
        message.status,
        target,
        connection_label(url),
    )


def _death_count(properties: pika.BasicProperties | None) -> int:
    if not properties or not properties.headers:
        return 0
    deaths = properties.headers.get("x-death")
    if not deaths:
        # custom retry header
        try:
            return int(properties.headers.get("x-retry-count") or 0)
        except (TypeError, ValueError):
            return 0
    total = 0
    for entry in deaths:
        try:
            total += int(entry.get("count") or 0)
        except (TypeError, ValueError):
            continue
    return total


def consume_forever(
    queue: str,
    on_message: Callable[[dict[str, Any], dict[str, Any]], None],
    *,
    url: str | None = None,
    prefetch: int = 1,
    max_requeue: int = MAX_REQUEUE,
) -> None:
    """Block-consuming loop with prefetch=1 and careful nack/requeue.

    ``on_message(payload, meta)`` should raise on transient failure.
    Permanent failures should be handled inside on_message (no raise) so we ack.
    """

    def _callback(ch: BlockingChannel, method, properties, body: bytes) -> None:
        meta = {
            "delivery_tag": method.delivery_tag,
            "redelivered": bool(method.redelivered),
            "retry_count": _death_count(properties),
            "routing_key": method.routing_key,
        }
        try:
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("message body must be a JSON object")
            on_message(payload, meta)
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as exc:
            retry = meta["retry_count"]
            logger.exception(
                "mq_consume_error queue=%s retry=%s err=%s",
                queue,
                retry,
                exc,
            )
            if retry >= max_requeue:
                ch.basic_ack(delivery_tag=method.delivery_tag)
                logger.error(
                    "mq_message_dropped_after_retries queue=%s retry=%s",
                    queue,
                    retry,
                )
            else:
                # Ack original + republish with incremented x-retry-count
                # (nack+requeue does not bump headers / x-death).
                headers = dict(properties.headers or {})
                headers["x-retry-count"] = retry + 1
                ch.basic_publish(
                    exchange="",
                    routing_key=queue,
                    body=body,
                    properties=pika.BasicProperties(
                        delivery_mode=2,
                        content_type=getattr(properties, "content_type", None)
                        or "application/json",
                        headers=headers,
                    ),
                )
                ch.basic_ack(delivery_tag=method.delivery_tag)

    with mq_connection(url) as conn:
        ch = conn.channel()
        declare_queues(ch)
        ch.queue_declare(queue=queue, durable=True)
        ch.basic_qos(prefetch_count=prefetch)
        ch.basic_consume(queue=queue, on_message_callback=_callback)
        logger.info(
            "mq_consume_start queue=%s prefetch=%s broker=%s",
            queue,
            prefetch,
            connection_label(url),
        )
        ch.start_consuming()


__all__ = [
    "MAX_REQUEUE",
    "connection_label",
    "consume_forever",
    "declare_queues",
    "mq_connection",
    "publish_json",
    "publish_result",
    "publish_task",
    "_reset_shared_publisher_for_tests",
]
