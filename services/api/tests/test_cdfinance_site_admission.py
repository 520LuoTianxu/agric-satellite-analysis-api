"""Unit tests for cdfinance groupSiteAdmission helpers."""

from __future__ import annotations

from app.core.cdfinance_site_admission import (
    normalize_admission_payload,
    unwrap_vendor_response,
    facts_for_assessment,
)
from app.core.agri_tags import (
    parse_cdfinance_group_id,
    ensure_cdfinance_group_tag,
)


def test_unwrap_envelope():
    raw = {"code": 200, "msg": "ok", "data": {"groupId": 3232, "score": 85.0}}
    d = unwrap_vendor_response(raw)
    assert d["groupId"] == 3232


def test_normalize_from_smoke_shape():
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
    assert n["group_id"] == "3232"
    assert n["score"] == 85.0
    assert n["total_area_mu"] == 310.0
    assert n["key_labels"]["soil_type"] == "沙壤"
    assert n["planned_crops"] == ["玉米"]
    assert n["dimensions"][0]["name"] == "土壤"
    facts = facts_for_assessment({**n, "fetched_at": "2026-09-14T00:00:00Z"})
    assert facts is not None
    assert facts["现场问卷_地块条件"]["soil_type"] == "沙壤"


def test_group_tags():
    assert parse_cdfinance_group_id(["agri:1", "cdfinance_group:3232"]) == "3232"
    assert parse_cdfinance_group_id(["group:99"]) == "99"
    tags = ensure_cdfinance_group_tag(["agri:1"], 3232)
    assert "cdfinance_group:3232" in tags
    assert ensure_cdfinance_group_tag(tags, 3232).count("cdfinance_group:3232") == 1
