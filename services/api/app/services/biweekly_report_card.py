"""Short conclusion card for the biweekly page.

The page should not render job_json. This keeps the few sentences a reader
can show, including the bold and the three colors the model already chose,
and a one-line status when those sentences are not stored yet.
"""

from __future__ import annotations

import re
from typing import Any

_TAG = re.compile(r"<[^>]+>")
_MAX_LINES = 4
# 与报告正文同一套三种强调色：向好、需留意、还不能判断。
_PALETTE = {
    "1F4D38": ("#1F4D38", "#D9F2E3"),
    "9C2B1B": ("#9C2B1B", "#F8D6D2"),
    "8A5A12": ("#8A5A12", "#F8E8C8"),
}
_FONT_OPEN = re.compile(
    r"<\s*font\b[^>]*\bcolor\s*=\s*['\"]#?(1F4D38|9C2B1B|8A5A12)['\"][^>]*>",
    re.IGNORECASE,
)
_BOLD_FACE = re.compile(
    r"<\s*font\b(?![^>]*\bcolor\s*=)[^>]*\bface\s*=\s*['\"]YaHei-Bold['\"][^>]*>",
    re.IGNORECASE,
)
_FONT_CLOSE = re.compile(r"<\s*/\s*font\s*>", re.IGNORECASE)
_BOLD_OPEN = re.compile(r"<\s*b\s*>", re.IGNORECASE)
_BOLD_CLOSE = re.compile(r"<\s*/\s*b\s*>", re.IGNORECASE)

_STATUS_LINE = {
    "queued": "报告已排队，完成后显示本期结论。",
    "running": "报告正在生成，完成后显示本期结论。",
    "dispatched": "报告正在生成，完成后显示本期结论。",
    "registration_pending": "报告文件已上传，正在登记，完成后显示本期结论。",
    "failed": "这期没有生成成功。",
    "evidence_limited": "本期没有可用的卫星观测，没有正式报告。",
    "ready": "报告已生成，可下载查看。",
}


def _visible(value: str) -> str:
    return " ".join(_TAG.sub("", value).split())


def _keep_emphasis(value: str) -> str:
    """Keep <b> and the three palette colors. Drop every other tag."""
    slots: list[str] = []

    def stash(html: str) -> str:
        slots.append(html)
        return f"\u0000{len(slots) - 1}\u0000"

    def font_open(match: re.Match[str]) -> str:
        color, background = _PALETTE[match.group(1).upper()]
        return stash(f'<font color="{color}" backColor="{background}">')

    text = _FONT_OPEN.sub(font_open, value)
    text = _BOLD_FACE.sub(lambda _match: stash("<b>"), text)
    text = _FONT_CLOSE.sub(lambda _match: stash("</font>"), text)
    text = _BOLD_OPEN.sub(lambda _match: stash("<b>"), text)
    text = _BOLD_CLOSE.sub(lambda _match: stash("</b>"), text)
    text = _TAG.sub("", text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    bold = 0
    font = 0

    def restore(match: re.Match[str]) -> str:
        nonlocal bold, font
        html = slots[int(match.group(1))]
        if html == "<b>":
            bold += 1
            return html
        if html == "</b>":
            if bold == 0:
                return ""
            bold -= 1
            return html
        if html.startswith("<font "):
            font += 1
            return html
        if html == "</font>":
            if font == 0:
                return ""
            font -= 1
            return html
        return ""

    text = re.sub(r"\u0000(\d+)\u0000", restore, text)
    return " ".join(text.split())


def _as_lines(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, list):
        raw = [item for item in value if isinstance(item, str)]
    else:
        return []
    lines: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = _keep_emphasis(item)
        visible = _visible(text)
        if not visible or visible in seen:
            continue
        seen.add(visible)
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
        note = _as_lines(narrative.get("reader_note"))
        if note:
            lines = note[:1]
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
