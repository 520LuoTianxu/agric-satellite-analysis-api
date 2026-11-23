"""JSON exception logs must carry a redacted stack, not exc_info=true."""

from __future__ import annotations

import io
import json
import logging
import unittest

import structlog

from agric_satellite_analysis_common.logging import processor_chain, redact_secrets


SECRET = "s3cret-pass"
ACCESS_KEY = "LTAIabcdef1234567890"


class RedactSecretsTests(unittest.TestCase):
    def test_redacts_assignments_uris_and_cloud_keys(self) -> None:
        raw = (
            "password=s3cret-pass token=abc.def "
            f"{ACCESS_KEY} "
            "postgresql://openfarm:s3cret-pass@db:5432/openfarm "
            "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"
        )
        cleaned = redact_secrets(raw)
        self.assertNotIn(SECRET, cleaned)
        self.assertNotIn(ACCESS_KEY, cleaned)
        self.assertNotIn("postgresql://openfarm", cleaned)
        self.assertIn("password=[redacted]", cleaned)
        self.assertIn("[uri:redacted]", cleaned)
        self.assertIn("[access-key:redacted]", cleaned)


class ExceptionProcessorTests(unittest.TestCase):
    def test_json_log_has_type_message_and_stack_without_exc_info_bool(self) -> None:
        buf = io.StringIO()
        previous = structlog.get_config()
        structlog.configure(
            processors=processor_chain(),
            wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(file=buf),
            cache_logger_on_first_use=False,
        )
        try:
            log = structlog.get_logger("exc-redaction-fixture")
            try:
                raise RuntimeError(
                    f"bucket call failed password={SECRET} access_key_secret={ACCESS_KEY}"
                )
            except RuntimeError:
                log.exception(
                    "biweekly_report_failed",
                    land_id="32696",
                    biweekly_job_id="bj-b435c12f9a70f8a9",
                )
        finally:
            structlog.configure(
                processors=previous["processors"],
                wrapper_class=previous["wrapper_class"],
                context_class=previous["context_class"],
                logger_factory=previous["logger_factory"],
                cache_logger_on_first_use=previous["cache_logger_on_first_use"],
            )
        line = buf.getvalue().strip().splitlines()[-1]
        payload = json.loads(line)
        self.assertNotIn("exc_info", payload)
        self.assertEqual(payload["event"], "biweekly_report_failed")
        self.assertEqual(payload["exc_type"], "RuntimeError")
        self.assertIn("bucket call failed", payload["exc_message"])
        self.assertNotIn(SECRET, payload["exc_message"])
        self.assertNotIn(ACCESS_KEY, payload["exc_message"])
        self.assertIn("Traceback", payload["exc_stack"])
        self.assertIn("RuntimeError", payload["exc_stack"])
        self.assertNotIn(SECRET, payload["exc_stack"])
        self.assertNotIn(ACCESS_KEY, payload["exc_stack"])
        blob = json.dumps(payload)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(ACCESS_KEY, blob)


if __name__ == "__main__":
    unittest.main()
