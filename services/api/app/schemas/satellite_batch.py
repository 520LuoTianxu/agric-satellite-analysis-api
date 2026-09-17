"""按规范地块清单提交遥感聚合回填。"""

from calendar import monthrange
from datetime import date
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator


class SatelliteBatchRequest(BaseModel):
    land_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        min_length=1,
        max_length=1000,
        validation_alias=AliasChoices("landIdList", "landIdlist", "land_ids"),
        serialization_alias="landIdList",
    )
    months: int = Field(default=36, ge=1, le=120)
    date_from: date | None = None
    date_to: date = Field(default_factory=date.today)
    force: bool = False
    sensors: list[Literal["S1", "S2"]] = Field(
        default_factory=lambda: ["S1", "S2"], min_length=1, max_length=2
    )

    @field_validator("land_ids", mode="before")
    @classmethod
    def normalize_land_ids(cls, value):
        # 农业系统可能传数字编号；统一成主表字符串键并去重，避免重复下载。
        if isinstance(value, list):
            if len(value) > 1000:
                raise ValueError("landIdList最多包含1000个地块")
            if any(
                isinstance(item, bool) or not isinstance(item, (str, int))
                for item in value
            ):
                raise ValueError("landIdList必须包含字符串或整数编号")
            return list(dict.fromkeys(str(item).strip() for item in value))
        return value

    @field_validator("sensors")
    @classmethod
    def unique_sensors(cls, value):
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_dates(self):
        if self.date_from is None:
            # 默认按日历回溯三年，避免36×30天少拉十多天；闰日或月末取目标月最后一天。
            year, month_index = divmod(
                self.date_to.year * 12 + self.date_to.month - 1 - self.months, 12
            )
            month = month_index + 1
            day = min(self.date_to.day, monthrange(year, month)[1])
            self.date_from = date(year, month, day)
        if self.date_from > self.date_to:
            raise ValueError("date_from必须不晚于date_to")
        if (self.date_to - self.date_from).days > 3660:
            raise ValueError("回填时间范围最多10年")
        return self


class SatelliteBatchGroup(BaseModel):
    anchor_land_id: str
    land_ids: list[str]
    aggregation_bbox: tuple[float, float, float, float]
    download_bbox: tuple[float, float, float, float]
    oversized: bool = False
    job_ids: list[str] = Field(default_factory=list)


class SatelliteBatchResponse(BaseModel):
    status: str = "queued"
    land_count: int
    group_count: int
    job_count: int
    date_from: date
    date_to: date
    groups: list[SatelliteBatchGroup]
