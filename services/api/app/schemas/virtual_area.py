"""虚拟项目区初始化与历史回填请求模型。"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator

Sensor = Literal["S1", "S2"]


class VirtualAreaOperationRequest(BaseModel):
    land_ids: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("landIdList", "land_ids"),
        description="为空表示全部有效地块；传值时只规划这些地块。",
    )
    date_from: date | None = None
    date_to: date | None = None
    years: Annotated[int, Field(ge=1, le=10)] = 5
    sensors: list[Sensor] = Field(default_factory=lambda: ["S1", "S2"])
    force: bool = False

    @field_validator("land_ids")
    @classmethod
    def normalize_land_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        result = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if not result:
            raise ValueError("land_ids 不能为空列表；不传表示全部地块")
        return result


class VirtualAreaOperationOut(BaseModel):
    status: str
    parent_job_id: str
    land_count: int = 0
    skipped_land_count: int = 0
    skipped_land_ids: list[str] = Field(default_factory=list)
    new_area_count: int = 0
    matched_land_count: int = 0
    area_count: int = 0
    job_count: int = 0
    queued_job_ids: list[str] = Field(default_factory=list)
    failed_job_ids: list[str] = Field(default_factory=list)
    area_ids: list[str] = Field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
