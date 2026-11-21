import unittest
from fixtures import source, narrative
from agric_satellite_analysis_common.biweekly.models import JobOptions, ReportError
from agric_satellite_analysis_common.biweekly.snapshot import build_snapshot
from agric_satellite_analysis_common.biweekly.narrative import (
    llm_payload,
    validate_narrative,
    template_narrative,
)


class NarrativeTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = build_snapshot(source())
        self.options = JobOptions(
            narrative_mode="injected", model_version="offline-fake-model-v1"
        )

    def test_payload_is_compact_and_does_not_disclose_scope_or_unusable_values(self):
        payload = llm_payload(self.snapshot)
        self.assertNotIn("tenant_id", payload)
        self.assertNotIn("rs.fvc", payload["facts"])
        self.assertNotIn("source_id", payload["facts"]["rs.ndvi"])
        self.assertIn("FVC_UNCALIBRATED", payload["limitations"])

    def test_validated_structured_numbers_and_mode_provenance(self):
        n = validate_narrative(narrative(), self.snapshot, self.options)
        self.assertEqual(n.mode, "injected")
        self.assertEqual(n.data["claims"][0]["values"][0]["value"], 0.64)
        self.assertEqual(n.model_version, "offline-fake-model-v1")

    def test_rejects_invented_numbers_unknown_refs_missing_evidence_and_unsafe_text(
        self,
    ):
        cases = []
        x = narrative()
        x["claims"][0]["values"][0]["value"] = 0.99
        cases.append(x)
        x = narrative()
        x["claims"][0]["evidence_refs"] = ["rs.fvc"]
        cases.append(x)
        x = narrative()
        x["claims"][0]["evidence_refs"] = ["unknown"]
        cases.append(x)
        x = narrative()
        x["claims"][0]["values"][0]["unit"] = "%"
        cases.append(x)
        x = narrative()
        x["summary"] = "预计增产30%"
        cases.append(x)
        x = narrative()
        x["summary"] = "预计增产三成"
        cases.append(x)
        x = narrative()
        x["summary"] = "无风险，无需核查"
        cases.append(x)
        x = narrative()
        x["claims"][0]["values"][0]["value"] = True
        cases.append(x)
        x = narrative()
        x["url"] = "https://invented.invalid"
        cases.append(x)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ReportError):
                validate_narrative(value, self.snapshot, self.options)

    def test_template_is_explicitly_no_ai_even_with_missing_evidence(self):
        raw = source()
        raw["evidence"] = []
        raw["stages"] = []
        n = template_narrative(build_snapshot(raw), JobOptions())
        self.assertEqual(n.mode, "template")
        self.assertEqual(n.model_version, "none")
        self.assertIn("未调用AI", n.data["summary"])
