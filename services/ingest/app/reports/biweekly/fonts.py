"""Resolve Microsoft YaHei fonts bundled with the download-machine report package.

Official biweekly PDFs always use these two faces (or an explicit
BIWEEKLY_FONT_DIR override). Do not invent a substitute face.
"""

from __future__ import annotations

import os
from pathlib import Path

from agric_satellite_analysis_common.biweekly.models import ReportError

BUNDLED_FONT_DIR = Path(__file__).resolve().parent / "assets"
REGULAR_NAME = "msyh.ttf"
BOLD_NAME = "msyhbd.ttf"


def bundled_font_dir() -> Path:
    return BUNDLED_FONT_DIR


def font_search_dirs() -> list[Path]:
    dirs: list[Path] = []
    configured = (
        os.environ.get("BIWEEKLY_FONT_DIR")
        or os.environ.get("BIWEEKLY_FONTS_DIR")
        or ""
    ).strip()
    if configured:
        dirs.append(Path(configured))
    dirs.append(bundled_font_dir())
    seen: set[str] = set()
    out: list[Path] = []
    for item in dirs:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def resolve_yahei_fonts() -> tuple[Path, Path]:
    for directory in font_search_dirs():
        regular = directory / REGULAR_NAME
        bold = directory / BOLD_NAME
        if regular.is_file() and bold.is_file():
            return regular, bold
    raise ReportError("FONT_MISSING")
