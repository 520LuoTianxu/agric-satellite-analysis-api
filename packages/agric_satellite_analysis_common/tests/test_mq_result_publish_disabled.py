"""CLOUDAMQP_RESULT_PUBLISH default off: no TLS handshake on publish_result."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agric_satellite_analysis_common import mq
from agric_satellite_analysis_common.mq_results import publish_task_result
from agric_satellite_analysis_common.mq_schemas import ResultMessage


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("CLOUDAMQP_RESULT_PUBLISH", raising=False)
    mq._reset_shared_publisher_for_tests()
    yield
    mq._reset_shared_publisher_for_tests()


def test_result_publish_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("CLOUDAMQP_RESULT_PUBLISH", raising=False)
    with patch.object(mq, "settings") as settings:
        settings.cloudamqp_result_publish = False
        assert mq.result_publish_enabled() is False


def test_result_publish_enabled_env_on(monkeypatch):
    monkeypatch.setenv("CLOUDAMQP_RESULT_PUBLISH", "1")
    assert mq.result_publish_enabled() is True


def test_publish_result_noop_opens_no_connection(monkeypatch):
    monkeypatch.setenv("CLOUDAMQP_RESULT_PUBLISH", "0")
    opens = {"n": 0}

    def boom(_params):
        opens["n"] += 1
        raise AssertionError("must not open AMQP when result publish disabled")

    with (
        patch.object(mq.pika, "BlockingConnection", side_effect=boom),
        patch.object(mq, "publish_json") as publish_json,
    ):
        mq.publish_result(
            ResultMessage(
                task_id="skip-me",
                status="success",
                oss_urls={"a": "https://example.com/a.json"},
            )
        )
    assert opens["n"] == 0
    publish_json.assert_not_called()


def test_publish_task_result_skips_enrichment_and_does_not_raise(monkeypatch):
    monkeypatch.setenv("CLOUDAMQP_RESULT_PUBLISH", "0")
    with (
        patch(
            "agric_satellite_analysis_common.mq_results.collect_parcel_oss_urls",
            side_effect=AssertionError("must not collect when AMQP off"),
        ),
        patch(
            "agric_satellite_analysis_common.mq_results.upload_result_summary",
            side_effect=AssertionError("must not upload summary when AMQP off"),
        ),
        patch.object(mq.pika, "BlockingConnection", side_effect=AssertionError("no conn")),
    ):
        msg = publish_task_result(
            task_id="t-skip",
            status="success",
            land_id="29461",
            collect_parcel_urls=True,
            upload_summary_if_empty=True,
        )
    assert msg.task_id == "t-skip"
    assert msg.status == "success"
