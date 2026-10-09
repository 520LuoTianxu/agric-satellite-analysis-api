"""Paths for fonts and package assets."""

from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
FONTS_DIR = PACKAGE_DIR / "fonts"
FONT_PATH = FONTS_DIR / "wqy-zenhei.ttf"
ASSETS_DIR = PACKAGE_DIR / "assets"
# 选地报告封面底图（A4 竖版，标题/页脚/水印已在图中，中部留白用于填写地块信息）。
COVER_IMAGE_PATH = ASSETS_DIR / "report_cover.jpg"
