"""CloudAMQP (RabbitMQ via pika) helpers for the outer task bus.

Never log the full CLOUDAMQP_URL (contains password). Prefer
``connection_label()`` for diagnostics.
"""

from __future__ import annotations

import json
import logging
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


@contextmanager
def mq_connection(url: str | None = None):
    """Yield a BlockingConnection; always close on exit."""
    conn = pika.BlockingConnection(_params(url))
    try:
        yield conn
    finally:
        try:
            if conn.is_open:
                conn.close()
        except Exception:
            pass


def declare_queues(
    channel: BlockingChannel,
    *,
    task_queue: str | None = None,
    result_queue: str | None = None,
) -> tuple[str, str]:
    """Declare durable task + result queues. Returns (task_q, result_q)."""
    tq = task_queue or settings.cloudamqp_task_queue
    rq = result_queue or settings.cloudamqp_result_queue
    channel.queue_declare(queue=tq, durable=True)
    channel.queue_declare(queue=rq, durable=True)
    return tq, rq


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


def publish_task(
    message: TaskMessage | dict[str, Any],
    *,
    url: str | None = None,
    queue: str | None = None,
) -> None:
    """Publish a TaskMessage to the task queue (producer helper)."""
    if isinstance(message, dict):
        message = TaskMessage.model_validate(message)
    payload = message.model_dump(mode="json")
    with mq_connection(url) as conn:
        ch = conn.channel()
        tq, _ = declare_queues(ch)
        target = queue or tq
        declare_queues(ch)  # ensure both exist
        ch.queue_declare(queue=target, durable=True)
        publish_json(ch, target, payload)
    logger.info(
        "mq_task_published task_id=%s type=%s queue=%s broker=%s",
        message.task_id,
        message.type,
        queue or settings.cloudamqp_task_queue,
        connection_label(url),
    )


def publish_result(
    message: ResultMessage | dict[str, Any],
    *,
    url: str | None = None,
    queue: str | None = None,
) -> None:
    """Publish a ResultMessage to the result queue."""
    if isinstance(message, dict):
        message = ResultMessage.model_validate(message)
    payload = message.model_dump(mode="json")
    with mq_connection(url) as conn:
        ch = conn.channel()
        _, rq = declare_queues(ch)
        target = queue or rq
        ch.queue_declare(queue=target, durable=True)
        publish_json(ch, target, payload)
    logger.info(
        "mq_result_published task_id=%s status=%s queue=%s broker=%s",
        message.task_id,
        message.status,
        queue or settings.cloudamqp_result_queue,
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
]
