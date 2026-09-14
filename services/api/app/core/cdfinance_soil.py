"""Client for 中和农信 / cdfinance ``analyzeSoilV2`` soil fertility API.

Auth strategy
-------------
* **Bearer JWT** from the H5/login session is required (passed by caller;
  never stored in git / settings).
* Query signing (``timestamp`` / ``nonce`` / ``z_seller`` / ``sv`` / ``sign``)
  is used by the knhsellerMobile gateway (RSA-1024, ``sv=sv01``). The
  joint-venture web front does **not** generate these params, and smoke
  tests show the endpoint accepts JWT + headers **without** a sign query.
  Callers may still pass a prebuilt ``auth_query`` if a gateway starts
  enforcing signatures.

Coords format: ``lon,lat|lon,lat|...`` (polygon ring; closing point optional).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlencode

import httpx

from app.core.config import settings
from app.core.logging import logger

SOURCE_NAME = "cdfinance_analyzeSoilV2"

# Indicator codes we normalize (OpenAPI IndicatorResult.name)
_INDICATOR_MAP = {
    "TN": ("tn_g_kg", "g/kg", "全氮"),
    "AN": ("an_mg_kg", "mg/kg", "碱解氮"),
    "AP": ("ap_mg_kg", "mg/kg", "有效磷"),
    "AK": ("ak_mg_kg", "mg/kg", "速效钾"),
    "TP": ("tp_g_kg", "g/kg", "全磷"),
    "TK": ("tk_g_kg", "g/kg", "全钾"),
    "SOM": ("som_g_kg", "g/kg", "有机质"),
    "pH": ("ph", "", "酸碱度"),
}


def geojson_to_coords_string(geojson: dict[str, Any] | None) -> str:
    """Convert Polygon/MultiPolygon GeoJSON to vendor ``lon,lat|...`` string."""
    if not geojson or not isinstance(geojson, dict):
        raise ValueError("Missing geometry for soil NPK analysis")
    gtype = geojson.get("type")
    coords = geojson.get("coordinates")
    if not coords:
        raise ValueError("Geometry has no coordinates")

    ring: list
    if gtype == "Polygon":
        ring = coords[0]
    elif gtype == "MultiPolygon":
        ring = coords[0][0]
    else:
        raise ValueError(f"Unsupported geometry type: {gtype}")

    if len(ring) < 3:
        raise ValueError("Polygon needs at least 3 vertices")

    # Drop closing duplicate if present
    if ring[0] == ring[-1] and len(ring) > 3:
        ring = ring[:-1]

    parts: list[str] = []
    for pt in ring:
        if len(pt) < 2:
            continue
        lon, lat = float(pt[0]), float(pt[1])
        parts.append(f"{lon},{lat}")
    if len(parts) < 3:
        raise ValueError("Polygon needs at least 3 valid vertices")
    return "|".join(parts)


def normalize_bearer(token: str | None) -> str:
    """Strip optional ``Bearer `` prefix; raise if empty."""
    raw = (token or "").strip()
    if not raw:
        raise ValueError("Missing cdfinance Bearer token")
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw:
        raise ValueError("Missing cdfinance Bearer token")
    return raw


def parse_auth_query(auth_query: str | None) -> dict[str, str]:
    """Parse optional prebuilt gateway query (timestamp/nonce/z_seller/sv/sign)."""
    if not auth_query or not str(auth_query).strip():
        return {}
    q = str(auth_query).strip()
    if q.startswith("?"):
        q = q[1:]
    return {k: v for k, v in parse_qsl(q, keep_blank_values=True)}


def normalize_vendor_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract flat NPK columns + display helpers from analyzeSoilV2 JSON."""
    indicators = payload.get("indicators") or []
    by_name: dict[str, dict[str, Any]] = {}
    for item in indicators:
        if isinstance(item, dict) and item.get("name"):
            by_name[str(item["name"])] = item

    out: dict[str, Any] = {
        "tn_g_kg": None,
        "an_mg_kg": None,
        "ap_mg_kg": None,
        "ak_mg_kg": None,
        "tp_g_kg": None,
        "tk_g_kg": None,
        "som_g_kg": None,
        "ph": None,
        "sqi_score": None,
        "sqi_rating": None,
        "texture_usda_cn": None,
        "vendor_log_id": None,
        "indicators": [],
    }

    for code, (field, _unit, name_cn) in _INDICATOR_MAP.items():
        item = by_name.get(code)
        if not item:
            continue
        try:
            val = float(item["value"]) if item.get("value") is not None else None
        except (TypeError, ValueError):
            val = None
        out[field] = val
        out["indicators"].append(
            {
                "code": code,
                "name_cn": item.get("name_cn") or name_cn,
                "value": val,
                "unit": item.get("unit"),
                "grade": item.get("grade"),
                "grade_level": item.get("grade_level"),
                "sqi_score": item.get("sqi_score"),
            }
        )

    sqi = payload.get("sqi") or {}
    if isinstance(sqi, dict):
        try:
            out["sqi_score"] = (
                float(sqi["total_score"]) if sqi.get("total_score") is not None else None
            )
        except (TypeError, ValueError):
            out["sqi_score"] = None
        out["sqi_rating"] = sqi.get("rating")

    texture = payload.get("texture") or {}
    if isinstance(texture, dict):
        out["texture_usda_cn"] = texture.get("usda_cn") or texture.get("usda_en")

    log_id = payload.get("logId")
    if log_id is not None:
        try:
            out["vendor_log_id"] = int(log_id)
        except (TypeError, ValueError):
            out["vendor_log_id"] = None

    # Convenience aliases for UI / PDF (氮/磷/钾 = available N/P/K)
    out["n"] = {
        "label": "氮",
        "tn_g_kg": out["tn_g_kg"],
        "an_mg_kg": out["an_mg_kg"],
    }
    out["p"] = {"label": "磷", "ap_mg_kg": out["ap_mg_kg"], "tp_g_kg": out["tp_g_kg"]}
    out["k"] = {"label": "钾", "ak_mg_kg": out["ak_mg_kg"], "tk_g_kg": out["tk_g_kg"]}
    return out


def build_analysis_body(
    *,
    coords: str,
    province_code: str = "",
    city_code: str = "",
    district_code: str = "",
    province_name: str = "",
    city_name: str = "",
    district_name: str = "",
    land_id: str | int | None = None,
    source: int = 2,
    user_id: str = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "coords": coords,
        "provinceName": province_name or "",
        "cityName": city_name or "",
        "districtName": district_name or "",
        "provinceCode": province_code or "",
        "cityCode": city_code or "",
        "districtCode": district_code or "",
        "userId": user_id or "",
        "source": source,
    }
    if land_id is not None and str(land_id).strip():
        try:
            body["landId"] = int(str(land_id).strip())
        except ValueError:
            # Non-numeric agri ids still omit landId
            pass
    return body


async def analyze_soil_v2(
    *,
    bearer_token: str,
    body: dict[str, Any],
    auth_query: str | None = None,
    hr_base_id: str | None = None,
    timeout: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """POST analyzeSoilV2; return raw JSON dict. Raises httpx / ValueError."""
    token = normalize_bearer(bearer_token)
    params = parse_auth_query(auth_query)
    base = settings.cdfinance_soil_base_url.rstrip("/")
    path = "/agriculture/land/analyzeSoilV2"
    url = f"{base}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"

    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "channel-net": settings.cdfinance_channel_net or "H5",
        "x-cfpamf-app-key": settings.cdfinance_app_key,
        "origin": settings.cdfinance_origin,
        "referer": settings.cdfinance_referer,
    }
    base_id = hr_base_id if hr_base_id is not None else settings.cdfinance_hr_base_id
    if base_id:
        headers["hr-base-id"] = str(base_id)

    to = timeout if timeout is not None else float(settings.cdfinance_soil_timeout_seconds)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=to)
    try:
        logger.info(
            "cdfinance_analyze_soil_v2",
            land_id=body.get("landId"),
            has_sign_query=bool(params.get("sign")),
            # never log token
        )
        resp = await http.post(url, headers=headers, content=json.dumps(body))
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected analyzeSoilV2 response type")
        # Some gateways wrap as {success, data}
        if "indicators" not in data and isinstance(data.get("data"), dict):
            data = data["data"]
        if "indicators" not in data:
            raise ValueError("analyzeSoilV2 response missing indicators")
        return data
    finally:
        if owns_client:
            await http.aclose()
