"""Process-wide MQ publish: one shared connection under a lock."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

import pytest

from openfarm_common import mq
from openfarm_common.mq_schemas import ResultMessage


@pytest.fixture(autouse=True)
def _reset_mq_shared_state():
    mq._reset_shared_publisher_for_tests()
    yield
    mq._reset_shared_publisher_for_tests()


def _fake_blocking_connection_factory(counter: list[int]):
    def factory(_params):
        counter[0] += 1
        conn = MagicMock()
        conn.is_open = True
        ch = MagicMock()
        ch.is_open = True
        conn.channel.return_value = ch
        return conn

    return factory


def test_parallel_publish_result_opens_single_connection():
    opens = [0]
    msgs = [
        ResultMessage(
            task_id=f"t-{i}",
            status="success",
            oss_urls={f"k{i}": f"https://example.com/{i}.json"},
            land_id="4863",
        )
        for i in range(12)
    ]

    with (
        patch.object(mq, "settings") as settings,
        patch.object(mq.pika, "BlockingConnection", side_effect=_fake_blocking_connection_factory(opens)),
        patch.object(mq, "declare_queues", return_value=("openfarm_download", "openfarm_process")),
        patch.object(mq, "publish_json") as publish_json,
    ):
        settings.cloudamqp_url = "amqps://u:p@example/vhost"
        settings.cloudamqp_download_queue = "openfarm_download"
        settings.cloudamqp_process_queue = "openfarm_process"

        def _one(msg):
            mq.publish_result(msg)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(_one, m) for m in msgs]
            for f in as_completed(futs):
                f.result()

    assert opens[0] == 1, f"expected 1 connection, got {opens[0]}"
    assert publish_json.call_count == 12


def test_publish_retries_on_connection_limit_then_succeeds():
    opens = [0]
    attempts = {"n": 0}

    def flaky_publish(ch, queue, payload, *, persistent=True):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Exception(
                'ConnectionClosedByBroker: (530) "NOT_ALLOWED - connection limit (20) is reached"'
            )

    with (
        patch.object(mq, "settings") as settings,
        patch.object(mq.pika, "BlockingConnection", side_effect=_fake_blocking_connection_factory(opens)),
        patch.object(mq, "declare_queues", return_value=("openfarm_download", "openfarm_process")),
        patch.object(mq, "publish_json", side_effect=flaky_publish),
        patch.object(mq.time, "sleep", return_value=None),
    ):
        settings.cloudamqp_url = "amqps://u:p@example/vhost"
        settings.cloudamqp_download_queue = "openfarm_download"
        settings.cloudamqp_process_queue = "openfarm_process"

        mq.publish_result(
            ResultMessage(
                task_id="retry-me",
                status="success",
                oss_urls={"a": "https://example.com/a.json"},
            )
        )

    assert attempts["n"] == 3
    # reconnect after each failure + final success path may open more than 1
    assert opens[0] >= 1
