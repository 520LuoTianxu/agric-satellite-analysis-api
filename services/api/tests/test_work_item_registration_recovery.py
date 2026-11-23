"""Expired leases and exhausted claims must requeue registration, not fail generate."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.work_items import (
    MAX_WORK_ITEM_ATTEMPTS,
    has_recoverable_oss_artifact,
    lease_expiry_status,
    registration_retry_is_due,
    work_item_has_recoverable_registration,
)


OSS = {
    "bucket": "agric-test",
    "key": "reports/default/32696/biweekly-bj-ef25e6391ad7437b.pdf",
    "sha256": "a" * 64,
    "size_bytes": 100,
}


class RegistrationRecoveryHelperTests(unittest.TestCase):
    def test_oss_artifact_is_recognized(self):
        self.assertTrue(has_recoverable_oss_artifact({"oss_artifact": OSS}))
        self.assertFalse(
            has_recoverable_oss_artifact({"stage": "registration_pending"})
        )
        self.assertFalse(has_recoverable_oss_artifact({"oss_artifact": {"key": "x"}}))

    def test_work_item_progress_and_payload_both_count(self):
        item = SimpleNamespace(
            progress_json={"stage": "registration_pending", "oss_artifact": OSS},
            payload_json={},
        )
        self.assertTrue(work_item_has_recoverable_registration(item))
        item = SimpleNamespace(progress_json={}, payload_json={"oss_artifact": OSS})
        self.assertTrue(work_item_has_recoverable_registration(item))
        item = SimpleNamespace(
            progress_json={"stage": "waiting_for_backfill"}, payload_json={}
        )
        self.assertFalse(work_item_has_recoverable_registration(item))

    def test_retry_after_delays_but_does_not_drop_the_item(self):
        now = datetime(2026, 11, 23, 8, 40, tzinfo=timezone.utc)
        item = SimpleNamespace(
            progress_json={
                "stage": "registration_pending",
                "oss_artifact": OSS,
                "registration_retry_after": (now + timedelta(seconds=60)).isoformat(),
            },
            payload_json={},
        )
        self.assertFalse(registration_retry_is_due(item, now))
        self.assertTrue(registration_retry_is_due(item, now + timedelta(seconds=61)))
        self.assertTrue(work_item_has_recoverable_registration(item))

    def test_exhausted_attempts_still_count_as_recoverable(self):
        item = SimpleNamespace(
            attempts=MAX_WORK_ITEM_ATTEMPTS + 2,
            progress_json={"stage": "registration_pending", "oss_artifact": OSS},
            payload_json={},
            status="leased",
        )
        self.assertTrue(work_item_has_recoverable_registration(item))
        self.assertGreaterEqual(int(item.attempts), MAX_WORK_ITEM_ATTEMPTS)
        self.assertEqual(lease_expiry_status(item), "pending")

    def test_lease_expiry_fails_only_without_an_uploaded_artifact(self):
        bare = SimpleNamespace(
            attempts=MAX_WORK_ITEM_ATTEMPTS,
            progress_json={"stage": "waiting_for_backfill"},
            payload_json={},
        )
        self.assertEqual(lease_expiry_status(bare), "failed")
        uploaded = SimpleNamespace(
            attempts=MAX_WORK_ITEM_ATTEMPTS,
            progress_json={"stage": "registration_pending", "oss_artifact": OSS},
            payload_json={},
        )
        self.assertEqual(lease_expiry_status(uploaded), "pending")


if __name__ == "__main__":
    unittest.main()
