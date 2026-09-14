"""Unit tests for cdfinance analyzeSoilV2 client helpers."""

from __future__ import annotations

import pytest

from app.core.cdfinance_soil import (
    geojson_to_coords_string,
    normalize_bearer,
    normalize_vendor_payload,
    parse_auth_query,
)


def test_geojson_to_coords_drops_closing_point():
    gj = {
        "type": "Polygon",
        "coordinates": [
            [
                [116.3, 38.1],
                [116.31, 38.1],
                [116.31, 38.11],
                [116.3, 38.11],
                [116.3, 38.1],
            ]
        ],
    }
    s = geojson_to_coords_string(gj)
    assert s == "116.3,38.1|116.31,38.1|116.31,38.11|116.3,38.11"


def test_normalize_bearer():
    assert normalize_bearer("Bearer abc") == "abc"
    assert normalize_bearer("abc") == "abc"
    with pytest.raises(ValueError):
        normalize_bearer("")


def test_parse_auth_query():
    q = parse_auth_query("timestamp=1&nonce=x&sv=sv01&sign=abc%3D")
    assert q["timestamp"] == "1"
    assert q["sign"] == "abc="
    assert parse_auth_query(None) == {}


def test_normalize_vendor_payload():
    payload = {
        "logId": 17419,
        "indicators": [
            {
                "name": "TN",
                "value": 0.55,
                "unit": "g/kg",
                "grade": "差",
                "name_cn": "全氮",
            },
            {
                "name": "AP",
                "value": 2.47,
                "unit": "mg/kg",
                "grade": "差",
                "name_cn": "有效磷",
            },
            {
                "name": "AK",
                "value": 112.49,
                "unit": "mg/kg",
                "grade": "良好",
                "name_cn": "速效钾",
            },
        ],
        "sqi": {"rating": "四等", "total_score": 48.61},
        "texture": {"usda_cn": "粘壤土"},
    }
    n = normalize_vendor_payload(payload)
    assert n["tn_g_kg"] == 0.55
    assert n["ap_mg_kg"] == 2.47
    assert n["ak_mg_kg"] == 112.49
    assert n["n"]["label"] == "氮"
    assert n["texture_usda_cn"] == "粘壤土"
    assert n["vendor_log_id"] == 17419
