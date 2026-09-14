"""existing_agri_scene_dates prefers internal HTTP when enabled."""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Minimal stubs so pipeline import does not need full celery/raster stack.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ExistingAgriSceneDatesHttpTests(unittest.TestCase):
    def test_http_path(self) -> None:
        # Import late after path setup; pipeline has many deps — patch at call site.
        from app.tasks import pipeline as pipeline_mod

        session = MagicMock()
        with (
            patch(
                "openfarm_common.internal_api.internal_api_enabled", return_value=True
            ),
            patch(
                "openfarm_common.internal_api.agri_scene_dates",
                return_value=["2024-05-01", "2024-05-02"],
            ),
        ):
            # Re-bind names used inside the function via its import
            with patch.dict(
                "sys.modules",
                {
                    "openfarm_common.internal_api": SimpleNamespace(
                        agri_scene_dates=lambda *a, **k: ["2024-05-01", "2024-05-02"],
                        internal_api_enabled=lambda: True,
                    )
                },
            ):
                out = pipeline_mod.existing_agri_scene_dates(session, "L1", "S2")
        self.assertEqual(out, {date(2024, 5, 1), date(2024, 5, 2)})
        session.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
