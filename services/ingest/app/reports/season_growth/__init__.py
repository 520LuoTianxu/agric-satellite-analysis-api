# -*- coding: utf-8 -*-
"""Season growth (生育期长势) PDF report package."""

from __future__ import annotations

__all__ = ["generate_season_growth_pdf"]


def __getattr__(name: str):
    if name == "generate_season_growth_pdf":
        from app.reports.season_growth.service import generate_season_growth_pdf

        return generate_season_growth_pdf
    raise AttributeError(name)
