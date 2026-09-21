"""历史营销分析与页面近期分析的输入边界。"""

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator


class SeasonOverride(BaseModel):
    start_date: date
    end_date: date
    crop: str = Field(default="", max_length=80)


class ServiceEvent(BaseModel):
    land_id: str = Field(min_length=1, max_length=64)
    date: date
    action: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=1000)
    control_land_id: str | None = Field(default=None, max_length=64)
    window_days: int = Field(default=30, ge=7, le=60)


class InsightsRequest(BaseModel):
    land_ids: list[str] = Field(min_length=1, max_length=20)
    start_date: date
    end_date: date
    mode: Literal["historical", "recent"] = "historical"
    reference_year: int | None = Field(default=None, ge=2015, le=2100)
    brand_name: str = Field(default="乡合农业遥感", max_length=80)
    title: str = Field(default="地块历史分析简报", max_length=100)
    seasons: dict[str, SeasonOverride] = Field(default_factory=dict)
    events: list[ServiceEvent] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_period(self):
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if (
            self.start_date > self.end_date
            or (self.end_date - self.start_date).days > 550
        ):
            raise ValueError("请选择不超过 550 天的有效分析区间")
        if self.start_date < date(2015, 1, 1) or self.end_date > today:
            raise ValueError("分析日期须介于 2015 年与今天之间")
        if self.mode == "historical" and self.end_date >= today:
            raise ValueError("历史报告截止日期须早于今天；近期分析请使用页面模式")
        if len(set(self.land_ids)) != len(self.land_ids) or any(
            not x.strip() or len(x) > 64 for x in self.land_ids
        ):
            raise ValueError("地块 ID 不能为空、重复或超过 64 字符")
        if (
            self.reference_year is not None
            and self.reference_year >= self.start_date.year
        ):
            raise ValueError("对照年份须早于分析区间的起始年份")
        if set(self.seasons) - set(self.land_ids):
            raise ValueError("人工生育窗只能设置在已选地块上")
        for window in self.seasons.values():
            if (
                not self.start_date
                <= window.start_date
                <= window.end_date
                <= self.end_date
            ):
                raise ValueError("人工生育窗须位于分析区间内")
        for event in self.events:
            if (
                event.land_id not in self.land_ids
                or not self.start_date <= event.date <= self.end_date
            ):
                raise ValueError("农事记录须位于已选地块和分析区间内")
            if event.control_land_id and (
                event.control_land_id not in self.land_ids
                or event.control_land_id == event.land_id
            ):
                raise ValueError("对照地块须为另一个已选地块")
        return self
