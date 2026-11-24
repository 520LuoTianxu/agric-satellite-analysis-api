"""One bounded look at key-node photos.

The biweekly prose model stays the configured text model. Photos are sent
only when BAILIAN_VISION_MODEL names an image model. Signatures are not
stored; this function receives URLs that already had their query removed.
"""

from __future__ import annotations

import json
import os
from typing import Any

_MAX_IMAGES = 3


def attach_photo_review(
    facts: dict[str, Any], *, client: Any | None = None
) -> dict[str, Any]:
    photos = list(((facts.get("key_node_records") or {}).get("photos")) or [])
    urls = [
        str(item.get("url"))
        for item in photos
        if isinstance(item, dict) and item.get("url")
    ]
    if not urls:
        facts["photo_review"] = {"status": "none", "count": 0}
        return facts
    model = (os.environ.get("BAILIAN_VISION_MODEL") or "").strip()
    if not model:
        facts["photo_review"] = {"status": "not_reviewed", "count": len(urls)}
        return facts
    try:
        note = _ask(model, urls[:_MAX_IMAGES], client)
    except Exception as exc:  # noqa: BLE001 — 照片检查失败不影响正文和上传
        facts["photo_review"] = {
            "status": "failed",
            "count": len(urls),
            "error": type(exc).__name__,
        }
        return facts
    facts["photo_review"] = {"status": "reviewed", "count": len(urls), "note": note}
    return facts


def _ask(model: str, urls: list[str], client: Any | None) -> str:
    import httpx

    from app.reports.biweekly.narrative_ai import bailian_settings

    cfg = bailian_settings()
    if not cfg["api_key"]:
        raise RuntimeError("missing_api_key")
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "这些是农事关键节点上传的现场照片。只判断画面是否能看清田间情况。"
                "不要估计苗数，不要诊断病虫害，不要给灌溉施肥打药建议。"
                '只输出 JSON：{"usable":true,"note":"一句中文"}'
            ),
        }
    ]
    for url in urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    body = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": content}],
    }
    own = client is None
    http = client or httpx.Client(timeout=60)
    try:
        response = http.post(
            f"{cfg['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        response.raise_for_status()
        raw = response.json()
    finally:
        if own:
            http.close()
    text = ""
    message = (raw.get("choices") or [{}])[0].get("message") or {}
    content_out = message.get("content")
    if isinstance(content_out, str):
        text = content_out
    elif isinstance(content_out, list):
        text = "\n".join(
            str(part.get("text") or "")
            for part in content_out
            if isinstance(part, dict)
        )
    note = _note(text)
    if not note:
        raise ValueError("empty_photo_note")
    return note


def _note(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.removeprefix("json").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return ""
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return ""
    if not isinstance(parsed, dict):
        return ""
    note = str(parsed.get("note") or "").strip()
    usable = parsed.get("usable")
    if usable is False:
        prefix = "画面看不清。"
        return (prefix + note)[:80]
    return note[:80]
