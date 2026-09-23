"""预警重算任务只通过 Internal HTTP 调用 API。"""

import unittest
from unittest.mock import patch

from app.tasks.agri_alerts import evaluate_agri_rs_alerts_for_land


class AlertEvaluationHttpTests(unittest.TestCase):
    @patch("agric_satellite_analysis_common.internal_api.evaluate_land_alerts")
    @patch("agric_satellite_analysis_common.internal_api.internal_api_enabled", return_value=True)
    def test_forwards_evaluation_to_api(self, _enabled, evaluate):
        evaluate.return_value = {"status": "ok", "created": 2}
        result = evaluate_agri_rs_alerts_for_land("LAND-1", replace_open=True)
        self.assertEqual(result["created"], 2)
        evaluate.assert_called_once_with("LAND-1", replace_open=True)

    @patch("agric_satellite_analysis_common.internal_api.internal_api_enabled", return_value=False)
    def test_fails_closed_without_internal_api(self, _enabled):
        with self.assertRaisesRegex(RuntimeError, "API_BASE_URL"):
            evaluate_agri_rs_alerts_for_land("LAND-1")


if __name__ == "__main__":
    unittest.main()
