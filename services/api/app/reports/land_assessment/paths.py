"""Paths for fonts and package assets."""

from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
FONTS_DIR = PACKAGE_DIR / "fonts"
FONT_PATH = FONTS_DIR / "wqy-zenhei.ttf"
