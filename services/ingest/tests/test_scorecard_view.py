"""Unit tests for slim land-assessment scorecard (stdlib only)."""

from __future__ import annotations

import unittest

from app.reports.land_assessment.scorecard_view import (
    DIMENSION_KEYS,
    scorecard_public_view,
)


def _full_scorecard(**overrides):
    dims = [
        {
            "key": "crop",
            "name": "作物匹配",
            "score": 78.2,
            "light": "绿",
            "weight": "25%",
        },
        {
            "key": "soil",
            "name": "土壤条件",
            "score": 72,
            "light": "绿",
            "weight": "20%",
        },
        {
            "key": "vigor",
            "name": "生育期遥感长势",
            "score": 85.0,
            "light": "绿",
            "weight": "20%",
        },
        {
            "key": "weather",
            "name": "天气适宜",
            "score": 68.4,
            "light": "黄",
            "weight": "15%",
        },
        {
            "key": "wet_safety",
            "name": "抗渍/洪涝安全",
            "score": 80,
            "light": "绿",
            "weight": "10%",
        },
        {
            "key": "drought_safety",
            "name": "抗旱安全",
            "score": 74.1,
            "light": "绿",
            "weight": "10%",
        },
    ]
    payload = {
        "overall": {
            "score": 76.5,
            "grade": "较好",
            "light": "绿",
            "one_liner": "生育期长势尚可",
            "thinking": "internal",
        },
        "dimensions": dims,
        "confidence": {"score": 78, "plain": "内部说明"},
        "howto": "长文",
        "method": {"crop": "maize"},
    }
    payload.update(overrides)
    return payload


class ScorecardPublicViewTests(unittest.TestCase):
    def test_keeps_six_keys_in_weight_order(self):
        view = scorecard_public_view(_full_scorecard())
        self.assertIsNotNone(view)
        self.assertEqual([d["key"] for d in view["dimensions"]], list(DIMENSION_KEYS))
        self.assertEqual(view["overall"]["score"], 76.5)
        self.assertEqual(view["overall"]["light"], "绿")
        self.assertEqual(view["confidence"]["score"], 78.0)
        self.assertNotIn("thinking", view["overall"])
        self.assertNotIn("howto", view)
        self.assertNotIn("name", view["dimensions"][0])

    def test_idempotent_on_slim_copy(self):
        first = scorecard_public_view(_full_scorecard())
        second = scorecard_public_view(first)
        self.assertEqual(first, second)

    def test_missing_dimension_returns_none(self):
        payload = _full_scorecard()
        payload["dimensions"] = payload["dimensions"][:5]
        self.assertIsNone(scorecard_public_view(payload))

    def test_missing_overall_returns_none(self):
        payload = _full_scorecard()
        payload["overall"] = {"grade": "较好"}
        self.assertIsNone(scorecard_public_view(payload))

    def test_rejects_non_dict(self):
        self.assertIsNone(scorecard_public_view(None))
        self.assertIsNone(scorecard_public_view("nope"))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
