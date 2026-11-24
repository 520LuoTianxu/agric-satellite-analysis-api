"""Compact key-node forms from Smart workbox JSON.

The API reads agriculture_workbox_task_node. This module only parses rows
that have already been loaded. It does not open MySQL. Names, land titles
and photo signatures stay out of the report facts.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# 这些键是地块身份或表单流程，不是农事内容。
_SKIP = {
    "landid",
    "landname",
    "landdisplay",
    "areatext",
    "needaudit",
    "needregional",
    "step",
    "taskcode",
    "skipdefaults",
    "wellidx",
    "welln",
    "city",
}
_LABELS = {
    "formWater": "水电情况",
    "formSand": "出沙情况",
    "formRepairDate": "维修日期",
    "formWellName": "机井",
    "formSandRemark": "出沙说明",
    "formWaterRemark": "水电说明",
    "sowDate": "播种日期",
    "varietyText": "品种",
    "planRate": "计划播量",
    "soilType": "表单土壤类型",
    "soil": "土壤",
    "clod": "土块",
    "flat": "平整",
    "tool": "机具",
    "surveyDate": "调查日期",
    "gapLabel": "断垄",
    "rootLabel": "根系",
    "countInput": "基本苗",
    "roundLabel": "调查轮次",
    "brandName": "品种品牌",
    "otherVar": "其他品种",
    "sbDep": "播种深度",
    "tdDep": "施肥深度",
    "resowDate": "补种日期",
    "totalArea": "作业面积",
    "cornerResult": "边角处理",
    "reportReason": "镇压说明",
    "act": "是否镇压",
}
_STATUS = {
    "PENDING_BRANCH_REVIEW": "待分支验收",
    "PENDING_REGIONAL_REVIEW": "待区域审核",
    "PENDING_EXECUTE": "待执行",
    "COMPLETED": "已完成",
    "DONE": "已完成",
    "REJECTED": "已驳回",
    "CLOSED_OVERDUE": "逾期关闭",
}
# 生育阶段用字和关键节点任务的对应。只用于挑选，不判断农事好坏。
_STAGE_TASKS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "播种",
        frozenset(
            {
                "PRECISION_SOWING",
                "BASE_FERTILIZER",
                "DEEP_PLOUGHING",
                "ROTARY_HARROW",
                "POST_SOWING_ROLL",
                "STUBBLE_CRUSH_QUALITY_CHECK",
            }
        ),
    ),
    ("出苗", frozenset({"EMERGENCE_CHECK", "EMERGENCE_WATER", "PRECISION_SOWING"})),
    (
        "越冬",
        frozenset({"OVERWINTER_WATER", "SOIL_MOISTURE_CONFIRM", "EMERGENCE_CHECK"}),
    ),
    (
        "返青",
        frozenset({"OVERWINTER_WATER", "SOIL_MOISTURE_CONFIRM", "EMERGENCE_CHECK"}),
    ),
    ("苗", frozenset({"EMERGENCE_CHECK", "SEEDLING_ACCEPTANCE"})),
    ("墒", frozenset({"SOIL_MOISTURE_CONFIRM", "OVERWINTER_WATER"})),
)
_EARLY = frozenset({"播种", "出苗", "整地"})
_MAX_NODES = 8
_MAX_ANSWERS = 6
_MAX_PHOTOS = 6
_URL = re.compile(r"^https?://", re.I)


def workbox_season_keys(season_id: str, crop_name: str | None = None) -> list[str]:
    """Candidate crop_season_key values for one satellite season.

    Live smart_agric only has ``2026-WINTER-WHEAT``. A ``2025/26`` wheat
    season therefore tries both years. Non-wheat seasons are not guessed.
    """
    raw = str(season_id or "").strip()
    keys: list[str] = []
    if re.fullmatch(r"\d{4}-[A-Z0-9-]+", raw):
        keys.append(raw)
    match = re.fullmatch(r"(\d{4})/(\d{2})", raw)
    crop = str(crop_name or "")
    wheat = (not crop) or ("麦" in crop) or crop.lower() in {"wheat", "winter wheat"}
    if match and wheat:
        start_year = int(match.group(1))
        end_year = (start_year // 100) * 100 + int(match.group(2))
        if end_year < start_year:
            end_year += 100
        keys.append(f"{start_year}-WINTER-WHEAT")
        keys.append(f"{end_year}-WINTER-WHEAT")
    unique: list[str] = []
    for key in keys:
        if key not in unique:
            unique.append(key)
    return unique[:4]


def status_label(code: str | None) -> str:
    raw = str(code or "").strip()
    return _STATUS.get(raw, raw)


def parse_workbox_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Turn query rows into short answers and photo URLs without signatures."""
    nodes: list[dict[str, Any]] = []
    photos: list[dict[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        answers: list[dict[str, str]] = []
        node_photos: list[str] = []
        for source, column in (
            ("executor", "executor_json"),
            ("branch_manager", "branch_manager_json"),
            ("regional_manager", "regional_manager_json"),
        ):
            found, urls = _from_blob(row.get(column), source)
            for item in found:
                if len(answers) < _MAX_ANSWERS:
                    answers.append(item)
            node_photos.extend(urls)
        if not answers and not node_photos:
            continue
        task_code = str(row.get("task_code") or "")
        task_name = str(
            row.get("task_name_snapshot") or row.get("task_name") or task_code
        )
        nodes.append(
            {
                "task_code": task_code,
                "task_name": task_name,
                "task_status": status_label(row.get("task_status")),
                "submitted_on": _day(
                    row.get("submitted_at") or row.get("report_submitted_at")
                ),
                "available_on": _day(row.get("available_date")),
                "stage_code": str(row.get("stage_code") or ""),
                "answers": answers,
                "photo_count": len(node_photos),
            }
        )
        for url in node_photos:
            if len(photos) >= _MAX_PHOTOS:
                break
            photos.append({"task_code": task_code, "task_name": task_name, "url": url})
        if len(nodes) >= 16:
            break
    return {"nodes": nodes, "photos": photos}


def select_key_nodes(
    nodes: list[dict[str, Any]],
    *,
    period_start: str | None,
    period_end: str | None,
    stage_text: str | None,
) -> list[dict[str, Any]]:
    """Keep nodes in this period, or nodes of the current growth stage."""
    start = _parse(period_start)
    end = _parse(period_end)
    stage = str(stage_text or "")
    chosen: list[dict[str, Any]] = []
    earlier: list[dict[str, Any]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        item = dict(node)
        submitted = _parse(item.get("submitted_on"))
        available = _parse(item.get("available_on"))
        in_period = _inside(submitted, start, end) or _inside(available, start, end)
        if in_period:
            item["relevance"] = "in_period"
            chosen.append(item)
        elif _matches_stage(
            str(item.get("task_code") or ""), str(item.get("task_name") or ""), stage
        ):
            item["relevance"] = "stage"
            chosen.append(item)
        else:
            item["relevance"] = "earlier"
            earlier.append(item)
    if not chosen and any(word in stage for word in _EARLY):
        for item in earlier:
            if item.get("task_code") in _STAGE_TASKS[0][1]:
                chosen.append(item)
    return chosen[:_MAX_NODES]


def _matches_stage(task_code: str, task_name: str, stage_text: str) -> bool:
    if not stage_text:
        return False
    if task_name and task_name in stage_text:
        return True
    for word, codes in _STAGE_TASKS:
        if word in stage_text and task_code in codes:
            return True
    return False


def _from_blob(raw: Any, source: str) -> tuple[list[dict[str, str]], list[str]]:
    data = _loads(raw)
    if not isinstance(data, dict):
        return [], []
    submit = data.get("submitData")
    body = submit if isinstance(submit, dict) else data
    answers: list[dict[str, str]] = []
    photos: list[str] = []
    prefix = {
        "executor": "",
        "branch_manager": "分支验收",
        "regional_manager": "区域审核",
    }.get(source, "")
    for key, value in body.items():
        if _is_photo_key(str(key)):
            photos.extend(_urls(value))
            continue
        if str(key).lower() in _SKIP:
            continue
        text = _scalar(value)
        if not text:
            continue
        label = _LABELS.get(str(key), str(key))
        if prefix:
            label = f"{prefix}{label}"
        answers.append({"source": source, "label": label, "value": text[:80]})
    return answers, photos


def _loads(raw: Any) -> Any:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text in {"", "{}", "null"}:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _is_photo_key(key: str) -> bool:
    folded = key.lower()
    return "photo" in folded or "image" in folded or folded.endswith("pic")


def _urls(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        cleaned = _clean_url(value)
        if cleaned:
            found.append(cleaned)
    elif isinstance(value, dict):
        for key, item in value.items():
            if _is_photo_key(str(key)) or str(key).lower() in {
                "url",
                "src",
                "ossurl",
                "path",
            }:
                found.extend(_urls(item))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                found.extend(_urls(item))
            elif isinstance(item, dict):
                for key in ("url", "src", "ossUrl", "path", "fileUrl"):
                    if item.get(key):
                        found.extend(_urls(item.get(key)))
                        break
                else:
                    found.extend(_urls(item))
    unique: list[str] = []
    for url in found:
        if url not in unique:
            unique.append(url)
    return unique


def _clean_url(value: str) -> str:
    text = value.strip()
    if not _URL.match(text):
        return ""
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    # 查询串里常有签名。报告和模型请求都不保留。
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"null", "none"}:
            return ""
        if _URL.match(text):
            return ""
        return text
    if isinstance(value, list):
        parts = [_scalar(item) for item in value[:3]]
        return "、".join(part for part in parts if part)
    return ""


def _day(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return None


def _parse(value: Any) -> date | None:
    text = _day(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _inside(day: date | None, start: date | None, end: date | None) -> bool:
    if day is None or start is None or end is None:
        return False
    return start <= day <= end
