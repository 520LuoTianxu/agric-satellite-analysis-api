# -*- coding: utf-8 -*-
"""Download & extract optional uploaded materials (soft-fail)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any


MATERIAL_TEXT_CAP = 8000


def _safe_name(key: str) -> str:
    return Path(key).name or "material"


def _extract_txt(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_pdf(path: Path) -> str | None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        parts: list[str] = []
        for page in reader.pages[:30]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(parts).strip()
        return text or None
    except Exception:
        pass
    try:
        from pdfminer.high_level import extract_text

        text = (extract_text(str(path)) or "").strip()
        return text or None
    except Exception:
        return None


def extract_local_file(path: Path) -> dict[str, Any]:
    """Extract text from one local file; images only list filename."""
    name = path.name
    suffix = path.suffix.lower()
    meta: dict[str, Any] = {
        "filename": name,
        "suffix": suffix,
        "bytes": path.stat().st_size if path.exists() else 0,
        "text": None,
        "ok": False,
        "note": None,
    }
    if not path.exists():
        meta["note"] = "file_missing"
        return meta
    if suffix in {".txt", ".md", ".csv", ".json", ".log"}:
        try:
            meta["text"] = _extract_txt(path)
            meta["ok"] = True
        except Exception as exc:
            meta["note"] = f"text_extract_failed:{exc}"
        return meta
    if suffix == ".pdf":
        text = _extract_pdf(path)
        if text:
            meta["text"] = text
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
    storage: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Download OSS/local keys and concatenate extracted text (capped).

    Soft-fails per key; never raises for extract errors.
    """
    keys = [k for k in (keys or []) if isinstance(k, str) and k.strip()]
    if not keys:
        return "", []

    if storage is None:
        try:
            from openfarm_common.storage import get_storage

            storage = get_storage()
        except Exception:
            try:
                from app.core.storage import get_storage

                storage = get_storage()
            except Exception as exc:
                return "", [
                    {
                        "filename": _safe_name(k),
                        "key": k,
                        "ok": False,
                        "note": f"storage_unavailable:{exc}",
                    }
                    for k in keys
                ]

    excerpts: list[str] = []
    metas: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="season_mat_") as tmp:
        tmp_path = Path(tmp)
        for key in keys[:20]:
            name = _safe_name(key)
            dest = tmp_path / name
            meta: dict[str, Any] = {
                "filename": name,
                "key": key,
                "ok": False,
                "note": None,
                "text": None,
            }
            try:
                data = None
                if hasattr(storage, "get_bytes"):
                    data = storage.get_bytes(key)
                elif hasattr(storage, "download_file"):
                    storage.download_file(key, str(dest))
                    data = dest.read_bytes() if dest.exists() else None
                else:
                    meta["note"] = "storage_has_no_get_bytes"
                    metas.append(meta)
                    continue
                if data is None:
                    meta["note"] = "empty_download"
                    metas.append(meta)
                    continue
                dest.write_bytes(data if isinstance(data, (bytes, bytearray)) else bytes(data))
                extracted = extract_local_file(dest)
                meta.update(
                    {
                        "ok": extracted.get("ok"),
                        "note": extracted.get("note"),
                        "suffix": extracted.get("suffix"),
                        "bytes": extracted.get("bytes"),
                    }
                )
                if extracted.get("text"):
                    excerpts.append(f"### {name}\n{extracted['text']}")
                elif extracted.get("note") == "image_listed_only":
                    excerpts.append(f"### 图片材料: {name}")
                metas.append(meta)
            except Exception as exc:
                meta["note"] = f"download_failed:{exc}"
                metas.append(meta)

    joined = "\n\n".join(excerpts)
    if len(joined) > MATERIAL_TEXT_CAP:
        joined = joined[:MATERIAL_TEXT_CAP] + "\n…[truncated]"
    # Strip full text from metas to keep job progress small
    for m in metas:
        m.pop("text", None)
    return joined, metas
