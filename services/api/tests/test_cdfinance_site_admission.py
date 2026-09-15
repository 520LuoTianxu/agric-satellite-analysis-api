"""Unit tests for cdfinance groupSiteAdmission helpers."""

from __future__ import annotations

import unittest

from app.core.agri_tags import (
    ensure_cdfinance_group_tag,
    parse_cdfinance_group_id,
)
from app.core.cdfinance_site_admission import (
    facts_for_assessment,
    normalize_admission_payload,
    unwrap_vendor_response,
)


class CdfinanceSiteAdmissionHelpersTests(unittest.TestCase):
    def test_unwrap_envelope(self):
        raw = {"code": 200, "msg": "ok", "data": {"groupId": 3232, "score": 85.0}}
        d = unwrap_vendor_response(raw)
        self.assertEqual(d["groupId"], 3232)

    def test_normalize_from_smoke_shape(self):
        record = {
            "id": 43,
            "groupId": 3232,
            "baseId": 37,
            "status": "draft",
            "score": 85.0,
            "scoreBank": "land_site_score_water_200",
            "surveyId": 208,
            "answerId": 410,
            "avgYield": 1646.51,
            "muProfit": 96.51,
            "totalArea": 310.0,
            "scoreView": {
                "score": 85.0,
                "scoreComplete": True,
                "groups": [
                    {
                        "groupId": "g1",
                        "dimensions": [
                            {
                                "id": "soil",
                                "name": "土壤",
                                "score": 32,
                                "maxScore": 37,
                                "items": [
                                    {
                                        "id": "soil_type",
                                        "name": "土壤类型",
                                        "optionKey": "sandy_loam",
                                        "optionLabel": "沙壤",
                                        "score": 12,
                                        "maxScore": 15,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            "payload": {
                "answers": {
                    "plannedCrops": [{"name": "玉米", "id": "corn"}],
                    "redLineAnswers": {"rl1": "no"},
                    "assessmentScope": {
                        "groups": [
                            {
                                "itemAnswers": {
                                    "soil_type": "沙壤",
                                    "water_source": "水库",
                                    "drainage": "基本完善，总体通畅",
                                }
                            }
                        ]
                    },
                },
                "evaluate": {"plotCount": "1", "totalArea": "310"},
            },
        }
        n = normalize_admission_payload(record)
        self.assertEqual(n["group_id"], "3232")
        self.assertEqual(n["score"], 85.0)
        self.assertEqual(n["total_area_mu"], 310.0)
        self.assertEqual(n["key_labels"]["soil_type"], "沙壤")
        self.assertEqual(n["planned_crops"], ["玉米"])
        self.assertEqual(n["dimensions"][0]["name"], "土壤")
        facts = facts_for_assessment({**n, "fetched_at": "2026-09-14T00:00:00Z"})
        self.assertIsNotNone(facts)
        assert facts is not None
        self.assertEqual(facts["现场问卷_地块条件"]["soil_type"], "沙壤")

    def test_group_tags(self):
        self.assertEqual(
            parse_cdfinance_group_id(["agri:1", "cdfinance_group:3232"]), "3232"
        )
        self.assertEqual(parse_cdfinance_group_id(["group:99"]), "99")
        tags = ensure_cdfinance_group_tag(["agri:1"], 3232)
        self.assertIn("cdfinance_group:3232", tags)
        self.assertEqual(
            ensure_cdfinance_group_tag(tags, 3232).count("cdfinance_group:3232"), 1
        )


if __name__ == "__main__":
    unittest.main()


class CdfinanceSiteAdmissionHeaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_hr_base_id_override_sent_in_headers(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.core import cdfinance_site_admission as mod

        captured: dict = {}

        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"code": 200, "data": {"groupId": 7071, "score": 1.0}}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def get(self, url, headers=None):
                captured["url"] = url
                captured["headers"] = dict(headers or {})
                return FakeResp()

            async def aclose(self):
                return None

        with (
            patch.object(mod, "httpx") as httpx_mod,
            patch.object(mod.settings, "cdfinance_hr_base_id", "37"),
            patch.object(
                mod.settings,
                "cdfinance_soil_base_url",
                "https://example.test/agric-api",
            ),
            patch.object(mod.settings, "cdfinance_app_key", "app-key"),
            patch.object(mod.settings, "cdfinance_origin", "https://origin"),
            patch.object(mod.settings, "cdfinance_referer", "https://referer"),
            patch.object(mod.settings, "cdfinance_channel_net", "H5"),
            patch.object(mod.settings, "cdfinance_soil_timeout_seconds", 5),
        ):
            httpx_mod.AsyncClient = FakeClient
            data = await mod.fetch_group_site_admission(
                group_id=7071,
                bearer_token="tok",
                hr_base_id="10",
            )
        self.assertEqual(captured["headers"].get("hr-base-id"), "10")
        self.assertEqual(captured["headers"].get("authorization"), "Bearer tok")
        self.assertEqual(data.get("groupId"), 7071)

    async def test_hr_base_id_falls_back_to_settings(self):
        from unittest.mock import patch

        from app.core import cdfinance_site_admission as mod

        captured: dict = {}

        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"code": 200, "data": {"groupId": 1}}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def get(self, url, headers=None):
                captured["headers"] = dict(headers or {})
                return FakeResp()

            async def aclose(self):
                return None

        with (
            patch.object(mod, "httpx") as httpx_mod,
            patch.object(mod.settings, "cdfinance_hr_base_id", "37"),
            patch.object(
                mod.settings,
                "cdfinance_soil_base_url",
                "https://example.test/agric-api",
            ),
            patch.object(mod.settings, "cdfinance_app_key", "app-key"),
            patch.object(mod.settings, "cdfinance_origin", "https://origin"),
            patch.object(mod.settings, "cdfinance_referer", "https://referer"),
            patch.object(mod.settings, "cdfinance_channel_net", "H5"),
            patch.object(mod.settings, "cdfinance_soil_timeout_seconds", 5),
        ):
            httpx_mod.AsyncClient = FakeClient
            await mod.fetch_group_site_admission(group_id=1, bearer_token="tok")
        self.assertEqual(captured["headers"].get("hr-base-id"), "37")
