"""Short conclusion card for the biweekly page.

The page should not render job_json. This keeps the few sentences a reader
can show, and a one-line status when those sentences are not stored yet.
"""

from __future__ import annotations

import re
from typing import Any

_TAG = re.compile(r"<[^>]+>")
_MAX_LINES = 4

_STATUS_LINE = {
    "queued": "报告已排队，完成后显示本期结论。",
    "running": "报告正在生成，完成后显示本期结论。",
    "dispatched": "报告正在生成，完成后显示本期结论。",
    "registration_pending": "报告文件已上传，正在登记，完成后显示本期结论。",
    "failed": "这期没有生成成功。",
    "evidence_limited": "本期没有可用的卫星观测，没有正式报告。",
    "ready": "报告已生成，可下载查看。",
}


def _plain(value: str) -> str:
    text = _TAG.sub("", value)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )
    return " ".join(text.split())


def _as_lines(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, list):
        raw = [item for item in value if isinstance(item, str)]
    else:
        return []
    lines: list[str] = []
    for item in raw:
        text = _plain(item)
        if text and text not in lines:
            lines.append(text)
    return lines


def build_report_card(job: dict[str, Any]) -> dict[str, Any]:
    """Pick up to four conclusion sentences already stored on the official job."""
    status = str(job.get("status") or "")
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    narrative = result.get("ai_narrative") if isinstance(result.get("ai_narrative"), dict) else {}
    lines = _as_lines(narrative.get("conclusion"))[:_MAX_LINES]
    source = "conclusion" if lines else ""
    if not lines:
        summary = _as_lines(narrative.get("summary"))
        if summary:
            lines = summary[:1]
            source = "summary"
    if not lines:
        note = _plain(str(narrative.get("reader_note") or ""))
        if note:
            lines = [note]
            source = "reader_note"
    if not lines:
        lines = [_STATUS_LINE.get(status, "这期还没有可显示的结论。")]
        source = "status"
    return {
        "job_id": job.get("job_id"),
        "status": status,
        "title": "本期结论",
        "lines": lines,
        "source": source,
    }
