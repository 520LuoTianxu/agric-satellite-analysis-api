"""Conservative structured narrative boundary, never a model/provider selector."""

from __future__ import annotations

import math
import re

from .models import JobOptions, Narrative, ReportError, Snapshot, canonical

# 模型不得在自由文字中创造数值；精确数值由结构化value引用与渲染器负责。
NUMBERS = re.compile(
    r"\d|[%％]|百分之|千分之|[零〇一二三四五六七八九十百千万亿两]+(?:成|倍|亩|吨|毫米|天|度|个)"
)
UNSAFE = re.compile(
    r"增产|减产|产量|灾损|确诊|必然|保证|无风险|无需核查|已受旱|已受涝|重旱|显著|正常|良好|安全|改善|上升|下降|偏高|偏低|https?://|[<>]"
)


def llm_payload(snapshot: Snapshot) -> dict:
    data = snapshot.data
    return {
        "schema_version": "biweekly-narrative-input-v1",
        "crop": data["crop"],
        "window": data["window"],
        "planned_periods": [
            {k: p[k] for k in ("name", "phase", "days")}
            for p in data["covered_periods"]
        ],
        "phenology_basis": data["phenology_basis"],
        "facts": {
            eid: {k: value[k] for k in ("metric", "label", "value", "unit")}
            for eid, value in data["facts"].items()
        },
        "limitations": data["quality_flags"],
        "instruction": "仅返回summary和claims；每条claims含text/evidence_refs/values。文字不得含数字或未经证据支持的定性；values必须逐项精确引用事实。计划阶段不是实测物候。缺失不补造。",
    }


def _prose(text, limit=350):
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > limit
        or any(c.isnumeric() or ord(c) < 32 for c in text)
        or NUMBERS.search(text)
        or UNSAFE.search(text)
    ):
        raise ReportError("NARRATIVE_INVALID")


def validate_narrative(
    value: dict, snapshot: Snapshot, options: JobOptions
) -> Narrative:
    try:
        if not isinstance(value, dict) or set(value) != {"summary", "claims"}:
            raise ReportError("NARRATIVE_INVALID")
        _prose(value["summary"])
        if not isinstance(value["claims"], list) or len(value["claims"]) > 8:
            raise ReportError("NARRATIVE_INVALID")
        facts = snapshot.data["facts"]
        for claim in value["claims"]:
            if set(claim) != {"text", "evidence_refs", "values"}:
                raise ReportError("NARRATIVE_INVALID")
            _prose(claim["text"])
            refs = claim["evidence_refs"]
            if (
                not isinstance(refs, list)
                or not refs
                or len(refs) > 8
                or len(refs) != len(set(refs))
                or not set(refs) <= facts.keys()
            ):
                raise ReportError("NARRATIVE_INVALID")
            if not isinstance(claim["values"], list) or len(claim["values"]) > 8:
                raise ReportError("NARRATIVE_INVALID")
            seen = set()
            for fact in claim["values"]:
                if set(fact) != {"evidence_id", "value", "unit"}:
                    raise ReportError("NARRATIVE_INVALID")
                eid = fact["evidence_id"]
                if eid not in refs or eid in seen:
                    raise ReportError("NARRATIVE_INVALID")
                seen.add(eid)
                number = fact["value"]
                if (
                    type(number) not in (int, float)
                    or not math.isfinite(number)
                    or number != facts[eid]["value"]
                    or fact["unit"] != facts[eid]["unit"]
                ):
                    raise ReportError("NARRATIVE_INVALID")
        return Narrative(
            canonical(value),
            options.narrative_mode,
            options.model_version,
            options.prompt_version,
        )
    except (KeyError, TypeError, ValueError):
        raise ReportError("NARRATIVE_INVALID") from None


def template_narrative(snapshot: Snapshot, options: JobOptions) -> Narrative:
    refs = list(snapshot.data["facts"])[:1]
    payload = {
        "summary": "未调用AI。当前为离线模板说明，证据受限，需结合田间记录核查。",
        "claims": [],
    }
    if refs:
        payload["claims"].append(
            {
                "text": "建议复核所列证据与田间记录，勿将计划阶段视作实测物候。",
                "evidence_refs": refs,
                "values": [],
            }
        )
    return validate_narrative(payload, snapshot, options)
