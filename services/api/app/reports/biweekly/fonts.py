"""Resolve Microsoft YaHei fonts for official biweekly PDFs.

Do not invent a substitute face. If both regular and bold files are missing,
the renderer must fail rather than silently switch fonts.
"""

from __future__ import annotations

import os
from pathlib import Path

from agric_satellite_analysis_common.biweekly.models import ReportError


def font_search_dirs() -> list[Path]:
    dirs: list[Path] = []
    configured = (
        os.environ.get("BIWEEKLY_FONT_DIR")
        or os.environ.get("BIWEEKLY_FONTS_DIR")
        or ""
    ).strip()
    if configured:
        dirs.append(Path(configured))
    dirs.extend(
        [
            Path("/workspace/fonts"),
            Path("/usr/local/share/fonts/msyh"),
            Path("/usr/share/fonts/truetype/msyh"),
            Path("/usr/share/fonts/truetype/microsoft"),
        ]
    )
    # De-duplicate while keeping order.
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
    """Return (regular, bold) Microsoft YaHei TTF paths."""
    for directory in font_search_dirs():
        regular = directory / "msyh.ttf"
        bold = directory / "msyhbd.ttf"
        if regular.is_file() and bold.is_file():
            return regular, bold
    raise ReportError("FONT_MISSING")
