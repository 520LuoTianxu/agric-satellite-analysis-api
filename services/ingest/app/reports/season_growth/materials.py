# -*- coding: utf-8 -*-
"""Download & extract optional uploaded materials (soft-fail)."""

from __future__ import annotations

import codecs
import tempfile
from pathlib import Path
from typing import Any


MATERIAL_TEXT_CAP = 8000
MATERIAL_KEY_COUNT_CAP = 20
MATERIAL_KEY_BYTES_CAP = 1023
MATERIAL_OBJECT_BYTES_CAP = 20 * 1024 * 1024
MATERIAL_TOTAL_BYTES_CAP = 40 * 1024 * 1024


def _safe_name(key: str) -> str:
    """生成只用于报告展示的短文件名，不把OSS键直接用作本地路径。"""
    if len(key) > MATERIAL_KEY_BYTES_CAP:
        return "material"
    name = Path(key).name or "material"
    safe = "".join(
        ch if ord(ch) >= 32 and ch not in '/\\:*?"<>|%' else "_"
        for ch in name
    ).strip(" .") or "material"
    while len(safe.encode("utf-8", errors="replace")) > 180:
        safe = safe[:-1]
    return safe or "material"


def _is_safe_material_key(key: str, allowed_prefix: str) -> bool:
    """限制worker只能读取本次报告地块上传目录下的OSS材料对象。"""
    if len(key) > MATERIAL_KEY_BYTES_CAP or not allowed_prefix.endswith("/"):
        return False
    if not key.startswith(allowed_prefix) or "\\" in key:
        return False
    if any(
        ord(char) < 32 or ord(char) == 127 or char in {"?", "#", "%"}
        for char in key
    ):
        return False
    try:
        if len(key.encode("utf-8")) > MATERIAL_KEY_BYTES_CAP:
            return False
    except UnicodeEncodeError:
        return False
    return all(part and part not in {".", ".."} for part in key.split("/"))


def _extract_txt(path: Path, max_chars: int) -> tuple[str, bool]:
    """分块解码文本，只保留剩余字符预算及一个截断探测字符。"""
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            decoder = codecs.getincrementaldecoder(enc)(errors="strict")
            parts: list[str] = []
            char_count = 0

            def append_bounded(chunk: str) -> bool:
                nonlocal char_count
                remaining = max_chars + 1 - char_count
                if len(chunk) >= remaining:
                    parts.append(chunk[:remaining])
                    return True
                parts.append(chunk)
                char_count += len(chunk)
                return False

            with path.open("rb") as stream:
                while raw_chunk := stream.read(4096):
                    decoded = decoder.decode(raw_chunk)
                    if append_bounded(decoded):
                        return "".join(parts)[:max_chars], True
                if append_bounded(decoder.decode(b"", final=True)):
                    return "".join(parts)[:max_chars], True
            return "".join(parts), False
        except UnicodeDecodeError:
            continue
    return "", False


def _extract_pdf(path: Path, max_chars: int) -> tuple[str | None, bool]:
    """最多读取30页，并把返回的文本限制在调用方剩余字符预算内。"""
    if max_chars <= 0:
        return None, True
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        parts: list[str] = []
        char_count = 0
        truncated = page_count > 30
        for page in reader.pages[:30]:
            try:
                page_text = page.extract_text() or ""
            except Exception:
                continue
            if not page_text:
                continue
            separator_chars = 1 if parts else 0
            remaining = max_chars - char_count - separator_chars
            if remaining <= 0:
                truncated = True
                break
            if len(page_text) > remaining:
                parts.append(page_text[:remaining])
                truncated = True
                break
            parts.append(page_text)
            char_count += separator_chars + len(page_text)
        text = "\n".join(parts).strip()
        return (text or None), truncated
    except Exception:
        pass
    try:
        from pdfminer.high_level import extract_text

        text = (extract_text(str(path), maxpages=30) or "").strip()
        return text[:max_chars] or None, len(text) > max_chars
    except Exception:
        return None, False


def extract_local_file(
    path: Path, *, max_text_chars: int = MATERIAL_TEXT_CAP
) -> dict[str, Any]:
    """Extract bounded text from one local file; images only list filename."""
    name = path.name
    suffix = path.suffix.lower()
    meta: dict[str, Any] = {
        "filename": name,
        "suffix": suffix,
        "bytes": path.stat().st_size if path.exists() else 0,
        "text": None,
        "text_truncated": False,
        "ok": False,
        "note": None,
    }
    if not path.exists():
        meta["note"] = "file_missing"
        return meta
    if suffix in {".txt", ".md", ".csv", ".json", ".log"}:
        if max_text_chars <= 0:
            meta["note"] = "text_budget_exceeded"
            meta["text_truncated"] = True
            return meta
        try:
            meta["text"], meta["text_truncated"] = _extract_txt(path, max_text_chars)
            meta["ok"] = True
        except Exception as exc:
            meta["note"] = f"text_extract_failed:{str(exc)[:160]}"
        return meta
    if suffix == ".pdf":
        if max_text_chars <= 0:
            meta["note"] = "text_budget_exceeded"
            meta["text_truncated"] = True
            return meta
        text, truncated = _extract_pdf(path, max_text_chars)
        if text:
            meta["text"] = text
            meta["text_truncated"] = truncated
            meta["ok"] = True
        else:
            meta["note"] = "pdf_extract_unavailable_or_empty"
        return meta
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp"}:
        meta["ok"] = True
        meta["note"] = "image_listed_only"
        return meta
    meta["note"] = "unsupported_type"
    return meta


def download_material_keys(
    keys: list[str] | None,
    *,
    allowed_prefix: str,
    storage: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """按地块目录和累计字节预算读取材料，并限制送入报告模型的文本量。

    单条失败会记录简短状态并继续；超出数量、对象大小或总读取预算的材料不会下载。
    """
    if not isinstance(keys, list) or not keys:
        return "", []

    # 旧任务消息也只检查有限数量，避免队列中历史异常数据扩大worker开销。
    selected = keys[:MATERIAL_KEY_COUNT_CAP]
    omitted_count = max(0, len(keys) - MATERIAL_KEY_COUNT_CAP)
    metas: list[dict[str, Any]] = []
    safe_keys: list[str] = []
    seen: set[str] = set()
    for candidate in selected:
        if not isinstance(candidate, str):
            metas.append({"filename": "material", "ok": False, "note": "invalid_material_key"})
            continue
        if len(candidate) > MATERIAL_KEY_BYTES_CAP:
            metas.append(
                {"filename": "material", "ok": False, "note": "material_key_out_of_scope"}
            )
            continue
        key = candidate.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        if not _is_safe_material_key(key, allowed_prefix):
            metas.append(
                {"filename": _safe_name(key), "ok": False, "note": "material_key_out_of_scope"}
            )
            continue
        safe_keys.append(key)

    if omitted_count:
        metas.append(
            {
                "filename": "material",
                "ok": False,
                "note": f"material_count_limit_exceeded:{omitted_count}_skipped",
            }
        )
    if not safe_keys:
        return "", metas

    if storage is None:
        try:
            from agric_satellite_analysis_common.storage import get_storage

            storage = get_storage()
        except Exception:
            try:
                from app.core.storage import get_storage

                storage = get_storage()
            except Exception:
                storage = None
    if storage is None:
        metas.extend(
            {
                "filename": _safe_name(key),
                "key": key,
                "ok": False,
                "note": "storage_unavailable",
            }
            for key in safe_keys
        )
        return "", metas

    excerpts: list[str] = []
    text_chars = 0
    text_truncated = bool(omitted_count)
    remaining_read_bytes = MATERIAL_TOTAL_BYTES_CAP
    with tempfile.TemporaryDirectory(prefix="season_mat_") as tmp:
        tmp_path = Path(tmp)
        for index, key in enumerate(safe_keys):
            name = _safe_name(key)
            suffix = Path(name).suffix.lower()
            if not suffix or len(suffix) > 16 or not suffix[1:].isalnum():
                suffix = ""
            # OSS文件名不直接拼入临时路径，避免超长名、保留字符和同名覆盖。
            dest = tmp_path / f"{index:02d}{suffix}"
            meta: dict[str, Any] = {
                "filename": name,
                "key": key,
                "ok": False,
                "note": None,
                "text_truncated": False,
            }
            if remaining_read_bytes <= 0:
                meta["note"] = "aggregate_read_limit_exceeded"
                metas.append(meta)
                continue
            if not hasattr(storage, "get_bytes"):
                meta["note"] = "storage_has_no_bounded_read"
                metas.append(meta)
                continue

            # 失败读取按完整单对象上限计入预算，防止反复超大对象绕过累计限制。
            # get_bytes会额外读取1字节识别超限，因此该探测字节也占用累计预算。
            read_limit = min(MATERIAL_OBJECT_BYTES_CAP, remaining_read_bytes - 1)
            if read_limit <= 0:
                meta["note"] = "aggregate_read_limit_exceeded"
                metas.append(meta)
                continue
            budget_before_read = remaining_read_bytes
            remaining_read_bytes -= read_limit + 1
            try:
                data = storage.get_bytes(key, max_bytes=read_limit)
                if data is None:
                    remaining_read_bytes = budget_before_read
                    meta["note"] = "empty_download"
                    metas.append(meta)
                    continue
                if not isinstance(data, (bytes, bytearray, memoryview)):
                    data = bytes(data)
                if len(data) > read_limit:
                    meta["note"] = "storage_ignored_read_limit"
                    metas.append(meta)
                    continue
                remaining_read_bytes = budget_before_read - len(data)
                if not data:
                    meta["note"] = "empty_download"
                    metas.append(meta)
                    continue
                dest.write_bytes(data)

                separator = "\n\n" if excerpts else ""
                header = f"### {name}\n"
                available_text = max(
                    0, MATERIAL_TEXT_CAP - text_chars - len(separator) - len(header)
                )
                extracted = extract_local_file(dest, max_text_chars=available_text)
                meta.update(
                    {
                        "ok": extracted.get("ok"),
                        "note": extracted.get("note"),
                        "suffix": extracted.get("suffix"),
                        "bytes": extracted.get("bytes"),
                        "text_truncated": extracted.get("text_truncated", False),
                    }
                )
                if extracted.get("text"):
                    excerpt = f"{separator}{header}{extracted['text']}"
                    excerpts.append(excerpt)
                    text_chars += len(excerpt)
                elif extracted.get("note") == "image_listed_only":
                    image_line = f"{separator}### 图片材料: {name}"
                    if text_chars + len(image_line) <= MATERIAL_TEXT_CAP:
                        excerpts.append(image_line)
                        text_chars += len(image_line)
                    else:
                        text_truncated = True
                if extracted.get("text_truncated"):
                    text_truncated = True
                metas.append(meta)
            except Exception:
                # 不把存储服务异常原文写入报告任务进度，避免泄露对象存储内部信息。
                meta["note"] = "download_or_extract_failed"
                metas.append(meta)

    joined = "".join(excerpts)
    if text_truncated:
        marker = "\n…[truncated]"
        joined = joined[: max(0, MATERIAL_TEXT_CAP - len(marker))] + marker
    # 任务进度只保留材料摘要和状态，不持久化完整抽取文本。
    for meta in metas:
        meta.pop("text", None)
    return joined, metas
