"""Unit tests for request/task trace_id helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import httpx

from agric_satellite_analysis_common.mq_schemas import ResultMessage, TaskMessage
from agric_satellite_analysis_common.trace import (
    TRACE_HEADER,
    bind_trace_from_mapping,
    bind_trace_id,
    clear_trace_id,
    current_trace_id,
    extract_trace_id,
    get_or_create_trace_id,
    inject_trace_into_celery_headers,
    new_trace_id,
    normalize_trace_id,
    stamp_trace_on_payload,
)


class NormalizeTests(unittest.TestCase):
    def test_accepts_uuid(self) -> None:
        value = "7c2e9b1a-4d3f-4a11-8c00-aaaaaaaaaaaa"
        self.assertEqual(normalize_trace_id(value), value)

    def test_strips_and_rejects_empty(self) -> None:
        self.assertIsNone(normalize_trace_id("  "))
        self.assertIsNone(normalize_trace_id(None))
        self.assertIsNone(normalize_trace_id("bad id with spaces"))
        self.assertIsNone(normalize_trace_id("x" * 129))


class BindTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_trace_id()

    def test_bind_and_clear(self) -> None:
        bind_trace_id("trace-one")
        self.assertEqual(current_trace_id(), "trace-one")
        clear_trace_id()
        self.assertIsNone(current_trace_id())

    def test_get_or_create_reuses_bound_id(self) -> None:
        first = get_or_create_trace_id()
        second = get_or_create_trace_id()
        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_new_trace_id_is_unique(self) -> None:
        self.assertNotEqual(new_trace_id(), new_trace_id())


class PayloadTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_trace_id()

    def test_extract_from_top_level_or_extras(self) -> None:
        self.assertEqual(extract_trace_id({"trace_id": "a1"}), "a1")
        self.assertEqual(extract_trace_id({"extras": {"trace_id": "b2"}}), "b2")
        self.assertIsNone(extract_trace_id({"extras": {}}))

    def test_stamp_payload_uses_current_id(self) -> None:
        bind_trace_id("stamp-1")
        out = stamp_trace_on_payload({"land_id": "L1"})
        self.assertEqual(out["trace_id"], "stamp-1")
        self.assertEqual(out["land_id"], "L1")

    def test_stamp_does_not_overwrite(self) -> None:
        bind_trace_id("new")
        out = stamp_trace_on_payload({"trace_id": "kept"})
        self.assertEqual(out["trace_id"], "kept")

    def test_bind_from_mapping(self) -> None:
        bind_trace_from_mapping({"trace_id": "from-msg"})
        self.assertEqual(current_trace_id(), "from-msg")


class CeleryHeaderTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_trace_id()

    def test_injects_current_trace_into_headers(self) -> None:
        bind_trace_id("cel-1")
        headers: dict[str, object] = {}
        inject_trace_into_celery_headers(headers=headers)
        self.assertEqual(headers.get("trace_id"), "cel-1")

    def test_bind_from_celery_task_request(self) -> None:
        from agric_satellite_analysis_common.trace import bind_trace_from_celery_task

        task = MagicMock()
        task.request.headers = {"trace_id": "cel-2"}
        bind_trace_from_celery_task(task=task)
        self.assertEqual(current_trace_id(), "cel-2")


class HttpxHookTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_trace_id()

    def test_attaches_header_on_request(self) -> None:
        from agric_satellite_analysis_common.trace import attach_trace_header

        bind_trace_id("http-1")
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["header"] = request.headers.get(TRACE_HEADER, "")
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        with httpx.Client(
            base_url="http://api.test",
            transport=transport,
            event_hooks={"request": [attach_trace_header]},
        ) as client:
            client.get("/v1/internal/lands/L1")
        self.assertEqual(captured["header"], "http-1")


class SchemaTests(unittest.TestCase):
    def test_task_message_optional_trace_id(self) -> None:
        old = TaskMessage(task_id="t1", type="weather_backfill", land_id="L1")
        self.assertIsNone(old.trace_id)
        new = TaskMessage(
            task_id="t2",
            type="weather_backfill",
            land_id="L1",
            trace_id="tr-1",
        )
        dumped = new.model_dump(mode="json")
        self.assertEqual(dumped["trace_id"], "tr-1")

    def test_result_message_optional_trace_id(self) -> None:
        old = ResultMessage(task_id="t1", status="success")
        self.assertIsNone(old.trace_id)
        new = ResultMessage(task_id="t2", status="failed", trace_id="tr-2")
        self.assertEqual(new.trace_id, "tr-2")


if __name__ == "__main__":
    unittest.main()
