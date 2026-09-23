"""按规范地块清单或闭区间提交遥感聚合回填。"""

from calendar import monthrange
from datetime import date
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator


class SatelliteBatchRequest(BaseModel):
    land_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None,
        validation_alias=AliasChoices("landIdList", "landIdlist", "land_ids"),
        serialization_alias="landIdList",
    )
    from_land_id: str | int | None = Field(
        default=None,
        validation_alias=AliasChoices("fromLandId", "from_land_id", "land_id_from"),
    )
    to_land_id: str | int | None = Field(
        default=None,
        validation_alias=AliasChoices("toLandId", "to_land_id", "land_id_to"),
    )
    months: int = Field(default=36, ge=1, le=120)
    years: int | None = Field(default=None, ge=1, le=10)
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
        if value is None:
            return value
        if isinstance(value, list):
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
        has_list = self.land_ids is not None
        has_range = self.from_land_id is not None or self.to_land_id is not None
        if has_list == has_range:
            raise ValueError("请在landIdList和from_land_id/to_land_id中二选一")
        if has_range and (
            self.from_land_id is None
            or self.to_land_id is None
            or not str(self.from_land_id).strip()
            or not str(self.to_land_id).strip()
        ):
            raise ValueError("from_land_id和to_land_id必须同时提供")
        if has_list and not self.land_ids:
            raise ValueError("landIdList不能为空")
        if has_range:
            self._validate_land_id_range()
        if self.years is not None and self.date_from is not None:
            raise ValueError("years不能和date_from同时提供")
        if self.date_from is None:
            if self.years is not None:
                self.date_from = self._subtract_years(self.date_to, self.years)
            else:
                # 默认按日历月回溯，避免36×30天少拉十多天；闰日或月末取目标月最后一天。
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

    @staticmethod
    def _subtract_years(day: date, years: int) -> date:
        """按自然年回溯，2 月 29 日在目标年降级为 2 月 28 日。"""
        try:
            return day.replace(year=day.year - years)
        except ValueError:
            return day.replace(year=day.year - years, day=28)

    def _validate_land_id_range(self) -> None:
        """校验闭区间格式；地块数量上限由查询结果和任务编排层控制。"""
        try:
            start = int(self.from_land_id or "")
            end = int(self.to_land_id or "")
        except ValueError as exc:
            raise ValueError("from_land_id和to_land_id必须是数字编号") from exc
        if start > end:
            raise ValueError("from_land_id不能大于to_land_id")

    def resolved_land_ids(self) -> list[str]:
        """展开请求中的列表或闭区间，实际执行时再按存在地块截取。"""
        if self.land_ids is not None:
            return list(self.land_ids)
        start = int(self.from_land_id or "")
        end = int(self.to_land_id or "")
        return [str(value) for value in range(start, end + 1)]


class SatelliteBatchGroup(BaseModel):
    anchor_land_id: str
    land_ids: list[str]
    aggregation_bbox: tuple[float, float, float, float]
    download_bbox: tuple[float, float, float, float]
    processing_boundary_geojson: dict
    oversized: bool = False
    job_ids: list[str] = Field(default_factory=list)


class SatelliteBatchResponse(BaseModel):
    status: str = "queued"
    land_count: int
    group_count: int
    job_count: int
    requested_land_count: int | None = None
    selected_land_ids: list[str] = Field(default_factory=list)
    skipped_land_count: int = 0
    date_from: date
    date_to: date
    groups: list[SatelliteBatchGroup]


class SatelliteHistoryBackfillRequest(BaseModel):
    """手动历史回填参数；未传 landIdList 时处理全部有效地块。"""

    land_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None,
        validation_alias=AliasChoices("landIdList", "landIdlist", "land_ids"),
        serialization_alias="landIdList",
    )
    date_from: date | None = None
    date_to: date | None = None
    years: int = Field(default=5, ge=1, le=10)
    sensors: list[Literal["S1", "S2"]] = Field(
        default_factory=lambda: ["S1", "S2"], min_length=1, max_length=2
    )
    force: bool = False

    @field_validator("land_ids", mode="before")
    @classmethod
    def normalize_history_land_ids(cls, value):
        if value is None:
            return None
        if not isinstance(value, list) or any(
            isinstance(item, bool) or not isinstance(item, (str, int))
            for item in value
        ):
            raise ValueError("landIdList必须包含字符串或整数编号")
        normalized = list(dict.fromkeys(str(item).strip() for item in value))
        if not normalized or any(not item for item in normalized):
            raise ValueError("landIdList不能为空")
        return normalized

    @field_validator("sensors")
    @classmethod
    def unique_history_sensors(cls, value):
        return list(dict.fromkeys(value))
