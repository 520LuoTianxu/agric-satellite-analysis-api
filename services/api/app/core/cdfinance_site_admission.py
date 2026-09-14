"""Client for 中和农信 / cdfinance ``groupSiteAdmission`` questionnaire API.

Pre-flow site admission survey (plot physical conditions, water, infra,
social/red-line checks, historical yield). Auth mirrors ``cdfinance_soil``:
Bearer JWT at request time; optional gateway ``auth_query`` sign params.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.cdfinance_soil import normalize_bearer, parse_auth_query
from app.core.config import settings
from app.core.logging import logger

SOURCE_NAME = "cdfinance_groupSiteAdmission"


def unwrap_vendor_response(data: dict[str, Any] | list | None) -> dict[str, Any]:
    """Return the admission record dict from gateway/API envelope."""
    if not isinstance(data, dict):
        raise ValueError("Unexpected groupSiteAdmission response type")
    # {code, msg, data: {...}}
    if isinstance(data.get("data"), dict) and (
        "groupId" in data["data"]
        or "payload" in data["data"]
        or "score" in data["data"]
    ):
        return data["data"]
    if "groupId" in data or "payload" in data:
        return data
    raise ValueError("groupSiteAdmission response missing admission data")


def _first_group_item_answers(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Prefer assessmentScope.groups[0].itemAnswers; else top-level itemAnswers."""
    if not isinstance(payload, dict):
        return {}
    answers = payload.get("answers") if isinstance(payload.get("answers"), dict) else {}
    scope = answers.get("assessmentScope") if isinstance(answers, dict) else None
    if isinstance(scope, dict):
        groups = scope.get("groups") or []
        if groups and isinstance(groups[0], dict):
            ia = groups[0].get("itemAnswers")
            if isinstance(ia, dict) and ia:
                return dict(ia)
    ia = answers.get("itemAnswers") if isinstance(answers, dict) else None
    return dict(ia) if isinstance(ia, dict) else {}


def _dimension_scores(score_view: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(score_view, dict):
        return []
    groups = score_view.get("groups") or []
    if not groups or not isinstance(groups[0], dict):
        return []
    dims = groups[0].get("dimensions") or []
    out: list[dict[str, Any]] = []
    for d in dims:
        if not isinstance(d, dict):
            continue
        items = []
        for it in d.get("items") or []:
            if isinstance(it, dict):
                items.append(
                    {
                        "id": it.get("id"),
                        "name": it.get("name"),
                        "option_key": it.get("optionKey"),
                        "option_label": it.get("optionLabel"),
                        "score": it.get("score"),
                        "max_score": it.get("maxScore"),
                    }
                )
        out.append(
            {
                "id": d.get("id"),
                "name": d.get("name"),
                "score": d.get("score"),
                "max_score": d.get("maxScore"),
                "items": items,
            }
        )
    return out


def normalize_admission_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Flatten vendor admission record into summary fields for DB / LLM / UI."""
    payload = record.get("payload")
    if isinstance(payload, str):
        import json

        try:
            payload = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
    if not isinstance(payload, dict):
        payload = {}

    answers = payload.get("answers") if isinstance(payload.get("answers"), dict) else {}
    evaluate = (
        payload.get("evaluate") if isinstance(payload.get("evaluate"), dict) else {}
    )
    score_view = record.get("scoreView")
    if not isinstance(score_view, dict):
        score_view = (
            payload.get("scoreView")
            if isinstance(payload.get("scoreView"), dict)
            else {}
        )

    item_answers = _first_group_item_answers(payload)
    planned = answers.get("plannedCrops") if isinstance(answers, dict) else None
    planned_names: list[str] = []
    if isinstance(planned, list):
        for c in planned:
            if isinstance(c, dict) and c.get("name"):
                planned_names.append(str(c["name"]))
            elif isinstance(c, str):
                planned_names.append(c)

    red_line = answers.get("redLineAnswers") if isinstance(answers, dict) else None
    if not isinstance(red_line, dict):
        red_line = {}

    dims = _dimension_scores(score_view if isinstance(score_view, dict) else None)

    # Key physical labels for UI / PDF one-liner
    key_labels = {
        "soil_type": item_answers.get("soil_type"),
        "land_nature": item_answers.get("land_nature"),
        "soil_depth": item_answers.get("soil_depth"),
        "terrain": item_answers.get("terrain"),
        "land_shape": item_answers.get("land_shape"),
        "water_source": item_answers.get("water_source"),
        "water_config": item_answers.get("water_config"),
        "water_flow": item_answers.get("water_flow"),
        "water_quality": item_answers.get("water_quality"),
        "drainage": item_answers.get("drainage"),
        "power": item_answers.get("power"),
        "traffic": item_answers.get("traffic"),
        "ownership": item_answers.get("ownership"),
        "village_support": item_answers.get("village_support"),
        "theft_risk": item_answers.get("theft_risk"),
        "irrigate_cost": item_answers.get("irrigate_cost"),
    }
    key_labels = {k: v for k, v in key_labels.items() if v is not None}

    group_id = record.get("groupId")
    try:
        group_id_str = str(int(group_id)) if group_id is not None else None
    except (TypeError, ValueError):
        group_id_str = str(group_id) if group_id is not None else None

    return {
        "group_id": group_id_str,
        "vendor_id": record.get("id"),
        "base_id": record.get("baseId"),
        "status": record.get("status"),
        "editable": record.get("editable"),
        "score": record.get("score"),
        "score_bank": record.get("scoreBank") or payload.get("scoreBank"),
        "survey_id": record.get("surveyId"),
        "answer_id": record.get("answerId"),
        "avg_yield": record.get("avgYield"),
        "mu_profit": record.get("muProfit"),
        "total_area_mu": record.get("totalArea"),
        "persisted": record.get("persisted"),
        "submit_time": record.get("submitTime"),
        "item_answers": item_answers,
        "key_labels": key_labels,
        "red_line_answers": red_line,
        "planned_crops": planned_names,
        "dimensions": dims,
        "evaluate": {
            "plot_count": evaluate.get("plotCount"),
            "mosaic_count": evaluate.get("mosaicCount"),
            "total_area": evaluate.get("totalArea"),
            "lessor": evaluate.get("lessor"),
            "expected_price": evaluate.get("expectedPrice"),
        },
        "score_complete": bool(score_view.get("scoreComplete"))
        if isinstance(score_view, dict)
        else None,
    }


def facts_for_assessment(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """Compact facts block for land-assessment LLM (soft-absent → None)."""
    if not summary:
        return None
    return {
        "source": SOURCE_NAME,
        "group_id": summary.get("group_id"),
        "status": summary.get("status"),
        "score": summary.get("score"),
        "score_bank": summary.get("score_bank"),
        "total_area_mu": summary.get("total_area_mu"),
        "avg_yield": summary.get("avg_yield"),
        "mu_profit": summary.get("mu_profit"),
        "planned_crops": summary.get("planned_crops") or [],
        "现场问卷_地块条件": summary.get("key_labels") or {},
        "红线排查": summary.get("red_line_answers") or {},
        "维度得分": [
            {
                "name": d.get("name"),
                "score": d.get("score"),
                "max": d.get("max_score"),
                "items": [
                    {
                        "name": it.get("name"),
                        "answer": it.get("option_label"),
                        "score": it.get("score"),
                    }
                    for it in (d.get("items") or [])
                ],
            }
            for d in (summary.get("dimensions") or [])
        ],
        "fetched_at": summary.get("fetched_at"),
    }


async def fetch_group_site_admission(
    *,
    group_id: str | int,
    bearer_token: str,
    auth_query: str | None = None,
    hr_base_id: str | None = None,
    timeout: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET groupSiteAdmission/{groupId}; return unwrapped admission dict."""
    token = normalize_bearer(bearer_token)
    params = parse_auth_query(auth_query)
    gid = str(group_id).strip()
    if not gid:
        raise ValueError("Missing groupId")

    base = settings.cdfinance_soil_base_url.rstrip("/")
    path = f"/agriculture/groupSiteAdmission/{gid}"
    url = f"{base}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"

    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/x-www-form-urlencoded",
        "channel-net": settings.cdfinance_channel_net or "H5",
        "x-cfpamf-app-key": settings.cdfinance_app_key,
        "origin": settings.cdfinance_origin,
        "referer": settings.cdfinance_referer,
    }
    base_id = hr_base_id if hr_base_id is not None else settings.cdfinance_hr_base_id
    if base_id:
        headers["hr-base-id"] = str(base_id)

    to = (
        timeout
        if timeout is not None
        else float(settings.cdfinance_soil_timeout_seconds)
    )
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=to)
    try:
        logger.info(
            "cdfinance_group_site_admission",
            group_id=gid,
            has_sign_query=bool(params.get("sign")),
            # never log token
        )
        resp = await http.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("code") not in (None, 200, "200"):
            msg = data.get("msg") or data.get("message") or "upstream error"
            raise ValueError(f"groupSiteAdmission failed: {msg}")
        return unwrap_vendor_response(data if isinstance(data, dict) else None)
    finally:
        if owns_client:
            await http.aclose()
