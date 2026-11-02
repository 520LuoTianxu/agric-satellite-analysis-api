"""Claim reports only after base-data types are drained."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app import work_agent as wa


class ClaimReportsAfterBaseTests(unittest.TestCase):
    def test_default_types_put_base_data_before_reports(self) -> None:
        self.assertLess(
            wa.DEFAULT_TYPES.index("satellite_batch"),
            wa.DEFAULT_TYPES.index("assessment_report"),
        )
        self.assertIn("assessment_report", wa.REPORT_TYPES)
        self.assertIn("satellite_batch", wa.BASE_DATA_TYPES)

    def test_split_claim_types_separates_reports(self) -> None:
        with patch.object(
            wa,
            "claim_types",
            return_value=[
                "assessment_report",
                "satellite_batch",
                "weather_backfill",
                "season_growth_report",
            ],
        ):
            base, reports = wa.split_claim_types()
        self.assertEqual(base, ["satellite_batch", "weather_backfill"])
        self.assertEqual(reports, ["assessment_report", "season_growth_report"])

    def test_run_loop_claims_base_before_reports(self) -> None:
        client = MagicMock()
        calls: list[list[str] | None] = []

        def fake_claim(_client, types=None, limit=1):
            calls.append(list(types) if types is not None else None)
            if types and "satellite_batch" in types:
                return [
                    {
                        "id": "w1",
                        "type": "satellite_batch",
                        "attempts": 1,
                        "payload_json": {
                            "land_id": "1",
                            "extras": {"job_id": "j1"},
                        },
                    }
                ]
            return []

        # Simulate one loop iteration internals by calling the preference logic.
        with (
            patch.object(wa, "reports_after_base_enabled", return_value=True),
            patch.object(
                wa,
                "claim_types",
                return_value=["satellite_batch", "assessment_report"],
            ),
            patch.object(wa, "claim_batch", side_effect=fake_claim) as claim,
            patch.object(wa, "process_item") as process,
        ):
            base_types, report_types = wa.split_claim_types()
            items = claim(client, types=base_types, limit=1) if base_types else []
            if not items and report_types:
                items = claim(client, types=report_types, limit=1)
            for item in items:
                process(client, item)

        self.assertEqual(calls, [["satellite_batch"]])
        process.assert_called_once()
        self.assertEqual(process.call_args.args[1]["type"], "satellite_batch")

    def test_reports_claimed_only_when_base_empty(self) -> None:
        client = MagicMock()
        calls: list[list[str]] = []

        def fake_claim(_client, types=None, limit=1):
            calls.append(list(types or []))
            if types == ["assessment_report"]:
                return [
                    {
                        "id": "w2",
                        "type": "assessment_report",
                        "attempts": 1,
                        "payload_json": {"land_id": "2", "extras": {}},
                    }
                ]
            return []

        with (
            patch.object(wa, "reports_after_base_enabled", return_value=True),
            patch.object(
                wa,
                "claim_types",
                return_value=["satellite_batch", "assessment_report"],
            ),
            patch.object(wa, "claim_batch", side_effect=fake_claim),
            patch.object(wa, "process_item") as process,
        ):
            base_types, report_types = wa.split_claim_types()
            items = wa.claim_batch(client, types=base_types, limit=1) if base_types else []
            if not items and report_types:
                items = wa.claim_batch(client, types=report_types, limit=1)
            for item in items:
                process(client, item)

        self.assertEqual(calls, [["satellite_batch"], ["assessment_report"]])
        process.assert_called_once()
        self.assertEqual(process.call_args.args[1]["type"], "assessment_report")


if __name__ == "__main__":
    unittest.main()
