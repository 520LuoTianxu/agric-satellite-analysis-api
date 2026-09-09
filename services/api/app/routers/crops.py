"""Crop catalog endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.crops import list_crops

router = APIRouter()


class CropOut(BaseModel):
    key: str
    name: str
    name_zh: str
    season_months: list[int] = Field(default_factory=list)
    peak_months: list[int] = Field(default_factory=list)
    season_label_zh: str = ""


@router.get("/crops", response_model=list[CropOut])
async def get_crops() -> list[dict[str, Any]]:
    """List integrated crops for field binding / assessment."""
    return list_crops()
