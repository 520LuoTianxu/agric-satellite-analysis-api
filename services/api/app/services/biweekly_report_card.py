"""Short conclusion card for the biweekly page.

The page should not render job_json. This keeps the few sentences a reader
can show, including the bold and the three colors the model already chose,
and a one-line status when those sentences are not stored yet.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
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
        raw = []
        for item in value:
            if isinstance(item, str):
                raw.append(item)
            elif isinstance(item, dict):
                # 模型有时把一句放在 sentence / text，而不是纯字符串。
                for key in ("sentence", "text", "line"):
                    if isinstance(item.get(key), str):
                        raw.append(item[key])
                        break
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


def _infield_lines(value: Any) -> list[str]:
    if isinstance(value, dict):
        return _as_lines(list(value.values()))
    return _as_lines(value)


_NARRATIVE_RANK = {
    "": 0,
    "reader_note": 1,
    "notes": 2,
    "summary": 3,
    "changes": 4,
    "conclusion": 5,
}


def _narrative_lines(narrative: Any) -> tuple[list[str], str]:
    """Reader sentences from one stored judgment. Conclusion wins over other sections."""
    if not isinstance(narrative, dict):
        return [], ""
    conclusion = _as_lines(narrative.get("conclusion"))[:_MAX_LINES]
    if conclusion:
        return conclusion, "conclusion"
    changes = _as_lines(narrative.get("changes"))[:_MAX_LINES]
    if changes:
        return changes, "changes"
    summary = _as_lines(narrative.get("summary"))[:1]
    if summary:
        return summary, "summary"
    notes: list[str] = []
    seen: set[str] = set()
    for line in (
        _as_lines(narrative.get("field_notes"))
        + _infield_lines(narrative.get("infield"))
        + _as_lines(narrative.get("surrounding"))
        + _as_lines(narrative.get("weather"))
    ):
        visible = _visible(line)
        if not visible or visible in seen:
            continue
        seen.add(visible)
        notes.append(line)
        if len(notes) >= _MAX_LINES:
            break
    if notes:
        return notes, "notes"
    reader = _as_lines(narrative.get("reader_note"))[:1]
    if reader:
        return reader, "reader_note"
    return [], ""


def _fact_documents(job: dict[str, Any], extra: tuple[Any, ...]) -> list[dict[str, Any]]:
    """Places the report text may have been stored, besides the official result."""
    docs: list[dict[str, Any]] = []
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    if result:
        docs.append(result)
    facts = job.get("facts") if isinstance(job.get("facts"), dict) else None
    if facts:
        docs.append(facts)
    nested = result.get("facts") if isinstance(result.get("facts"), dict) else None
    if nested:
        docs.append(nested)
    for item in extra:
        if isinstance(item, dict):
            docs.append(item)
            inner = item.get("facts") if isinstance(item.get("facts"), dict) else None
            if inner:
                docs.append(inner)
    return docs


def _program_lines(docs: list[dict[str, Any]]) -> list[str]:
    """Short sentences the PDF prints even when the model wrote nothing."""
    lines: list[str] = []
    seen: set[str] = set()
    for doc in docs:
        chunks: list[str] = []
        context = doc.get("conclusion_context")
        if isinstance(context, dict):
            chunks.extend(_as_lines(context.get("lines")))
        patterns = doc.get("field_patterns")
        if isinstance(patterns, dict):
            chunks.extend(_as_lines(patterns.get("persistent_lines")))
            chunks.extend(_as_lines(patterns.get("uniformity_lines")))
            chunks.extend(_as_lines(patterns.get("history_line")))
        water = doc.get("water_risk")
        if isinstance(water, dict):
            chunks.extend(_as_lines(water.get("lines")))
        chunks.extend(_as_lines(doc.get("brief")))
        for line in chunks:
            visible = _visible(line)
            if not visible or visible in seen:
                continue
            seen.add(visible)
            lines.append(line)
            if len(lines) >= _MAX_LINES:
                return lines
    return lines


# 页面用已经入库的田块日均值。PDF 里的像元中位数没有写回 result，不能在这里冒充。
_PRIOR_DAYS = 14
_INDEX_ROWS = (
    ("ndvi", "NDVI", "长势"),
    ("evi", "EVI", "绿度"),
    ("ndre", "NDRE", "叶绿素"),
    ("ndmi", "NDMI", "叶片含水"),
)
_WEATHER_ROWS = (
    ("precipitation_mm", "有效降雨", "毫米"),
    ("et0_mm", "蒸散", "毫米"),
    ("mean_c", "平均气温", "℃"),
)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _day(value: Any) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _parameter_document(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """The stored facts that still have optical days or weather totals."""
    for doc in docs:
        if _rows(doc.get("observations")) or _rows(doc.get("prior_observations")):
            return doc
        period = doc.get("weather_period")
        if isinstance(period, dict) and period:
            return doc
        if _rows(doc.get("weather_daily")):
            return doc
    return {}


def _mean(rows: list[dict[str, Any]], metric: str) -> tuple[float | None, int]:
    values: list[float] = []
    for row in rows:
        number = _number(row.get(f"{metric}_avg"))
        if number is None:
            number = _number(row.get(metric))
        if number is not None:
            values.append(number)
    if not values:
        return None, 0
    return round(sum(values) / len(values), 2), len(values)


def _between(
    rows: list[dict[str, Any]], start: date | None, end: date | None
) -> list[dict[str, Any]]:
    if start is None or end is None:
        return []
    chosen = []
    for row in rows:
        day = _day(row.get("date"))
        if day is not None and start <= day <= end:
            chosen.append(row)
    return chosen


def _sum_mm(rows: list[dict[str, Any]], key: str) -> float | None:
    values = []
    for row in rows:
        number = _number(row.get(key))
        if number is not None:
            values.append(number)
    if not values:
        return None
    return round(sum(values), 1)


def _mean_temperature(rows: list[dict[str, Any]]) -> float | None:
    values = []
    for row in rows:
        low = _number(row.get("min_c"))
        high = _number(row.get("max_c"))
        if low is not None and high is not None:
            values.append((low + high) / 2)
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _window(
    period: dict[str, Any],
) -> tuple[date | None, date | None, date | None, date | None]:
    start = _day(period.get("start"))
    end = _day(period.get("end_inclusive")) or _day(period.get("end"))
    if start is not None and end is None:
        days = period.get("days")
        if isinstance(days, int) and days > 0:
            end = start + timedelta(days=days - 1)
    if start is None:
        return None, None, None, None
    prior_end = start - timedelta(days=1)
    prior_start = start - timedelta(days=_PRIOR_DAYS)
    return start, end, prior_start, prior_end


def _period_parameters(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Current period and the previous 14 days, from fields stored on the job."""
    doc = _parameter_document(docs)
    raw_period = doc.get("period")
    period = raw_period if isinstance(raw_period, dict) else {}
    current_start, current_end, prior_start, prior_end = _window(period)
    current_rows = _rows(doc.get("observations"))
    prior_rows = _rows(doc.get("prior_observations"))
    metrics = []
    for key, abbr, name in _INDEX_ROWS:
        current, current_days = _mean(current_rows, key)
        prior, prior_days = _mean(prior_rows, key)
        if current is None and prior is None:
            continue
        metrics.append(
            {
                "key": key,
                "abbr": abbr,
                "name": name,
                "unit": "",
                "decimals": 2,
                "current": current,
                "prior": prior,
                "current_days": current_days,
                "prior_days": prior_days,
            }
        )
    daily = _rows(doc.get("weather_daily"))
    raw_weather = doc.get("weather_period")
    stored = raw_weather if isinstance(raw_weather, dict) else {}
    current_daily = _between(daily, current_start, current_end)
    prior_daily = _between(daily, prior_start, prior_end)
    weather = []
    for key, name, unit in _WEATHER_ROWS:
        if key == "mean_c":
            current = _number(stored.get("mean_c"))
            if current is None:
                current = _mean_temperature(current_daily)
            else:
                current = round(current, 1)
            prior = _mean_temperature(prior_daily)
        else:
            current = _number(stored.get(key))
            if current is None:
                current = _sum_mm(current_daily, key)
            else:
                current = round(current, 1)
            prior = _sum_mm(prior_daily, key)
        if current is None and prior is None:
            continue
        weather.append(
            {
                "key": key,
                "name": name,
                "unit": unit,
                "decimals": 1,
                "current": current,
                "prior": prior,
            }
        )
    return {
        "periods": {
            "current": {
                "label": "本期",
                "start": _iso(current_start),
                "end": _iso(current_end),
            },
            "prior": {
                "label": "上一期",
                "start": _iso(prior_start),
                "end": _iso(prior_end),
            },
        },
        "metrics": metrics,
        "weather": weather,
    }


def build_report_card(job: dict[str, Any], *extra: Any) -> dict[str, Any]:
    """Up to four sentences a reader can show for this official job.

    ``extra`` documents are other stored copies of the same facts, such as the
    download task progress. They are used only when the official job itself
    has no reader text.
    """
    status = str(job.get("status") or "")
    docs = _fact_documents(job, extra)
    chosen: tuple[list[str], str] = ([], "")
    for doc in docs:
        found = _narrative_lines(doc.get("ai_narrative"))
        if _NARRATIVE_RANK[found[1]] > _NARRATIVE_RANK[chosen[1]]:
            chosen = found
    lines, source = chosen
    if not lines:
        lines = _program_lines(docs)
        source = "report" if lines else ""
    if not lines:
        lines = [_STATUS_LINE.get(status, "这期还没有可显示的结论。")]
        source = "status"
    card = {
        "job_id": job.get("job_id"),
        "status": status,
        "title": "本期结论",
        "lines": lines,
        "source": source,
    }
    card.update(_period_parameters(docs))
    return card
