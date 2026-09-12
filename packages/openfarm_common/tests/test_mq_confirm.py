"""Broker rejection must never acknowledge the original download task."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pika
import pytest

from openfarm_common import mq


def test_queues_enable_broker_confirmation():
    channel = MagicMock()
    mq.declare_queues(channel)
    channel.confirm_delivery.assert_called_once_with()


def test_publish_requires_routable_delivery():
    channel = MagicMock()
    channel.basic_publish.side_effect = pika.exceptions.UnroutableError([])
    with pytest.raises(pika.exceptions.UnroutableError):
        mq.publish_json(channel, "download", {"task_id": "one"})
    assert channel.basic_publish.call_args.kwargs["mandatory"] is True


@pytest.mark.parametrize("rejected", [False, True])
def test_retry_ack_follows_confirmed_publish(rejected):
    channel, connection = MagicMock(), MagicMock()
    connection.channel.return_value = channel
    order = []

    def publish(**kwargs):
        assert kwargs["mandatory"] is True
        assert kwargs["properties"].headers["x-retry-count"] == 1
        order.append("publish")
        if rejected:
            raise pika.exceptions.NackError([])

    channel.basic_publish.side_effect = publish
    channel.basic_ack.side_effect = lambda **kwargs: order.append("ack")

    def consume():
        callback = channel.basic_consume.call_args.kwargs["on_message_callback"]
        callback(
            channel,
            SimpleNamespace(delivery_tag=1, redelivered=False, routing_key="download"),
            pika.BasicProperties(headers={}),
            b"{}",
        )

    channel.start_consuming.side_effect = consume

    @contextmanager
    def connect(url):
        yield connection

    with patch.object(mq, "mq_connection", connect):
        handler = MagicMock(side_effect=RuntimeError("temporary failure"))
        if rejected:
            with pytest.raises(pika.exceptions.NackError):
                mq.consume_forever("download", handler)
        else:
            mq.consume_forever("download", handler)
    assert order == (["publish"] if rejected else ["publish", "ack"])
