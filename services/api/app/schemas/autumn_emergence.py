"""秋季出苗接口的返回形状。状态用稳定英文枚举，中文只做展示。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BaselineWindow(BaseModel):
    dates: list[date] = Field(default_factory=list)
    start: date | None = None
    end: date | None = None
    statistic: str | None = None


class EmergenceSummaryStats(BaseModel):
    n_clear: int | None = None
    n_pixels: int | None = None
    median_increment: float | None = None
    share_above_noise: float | None = None
    share_sustained: float | None = None
    share_ndvi_ge_0_30: float | None = Field(
        None,
        description="截至日 NDVI≥0.30 的像元比例，只对照产品参考刻度，不参与出苗判定。",
    )
    ndre_share_above_noise: float | None = Field(
        None,
        description="NDRE 高于其本底噪声的像元比例。缺红边时为空，不改变 NDVI 状态。",
    )
    class_counts: dict[str, int] | None = None
    reason: str | None = None


class EmergenceTimePoint(BaseModel):
    date: date
    in_baseline: bool
    n_pixels: int
    median_ndvi: float | None = None
    median_increment: float | None = None
    share_above_noise: float | None = None


class AutumnEmergenceOut(BaseModel):
    """地块×秋季的出苗摘要。field_id 与 land_id 相同。"""

    land_id: str
    field_id: str
    eligible: bool
    pending: bool = False
    season_key: str
    status: Literal["not_emerged", "emerging", "emerged", "uncertain"] | None = None
    status_zh: str | None = Field(None, description="未出苗 / 出苗中 / 已出苗 / 不确定")
    emerged_flag: bool = False
    frozen: bool = False
    baseline: BaselineWindow = Field(default_factory=BaselineWindow)
    noise_floor: float | None = Field(
        None, description="本块本季实际使用的抬升阈值，不是固定的 0.035 或 0.30。"
    )
    noise_statistic: str | None = None
    as_of_date: date | None = None
    first_emerged_date: date | None = Field(
        None,
        description="第一次满足持续抬升规则的晴空产品日，不是农学出苗日。",
    )
    method_version: str
    summary: EmergenceSummaryStats = Field(default_factory=EmergenceSummaryStats)
    timeseries: list[EmergenceTimePoint] = Field(default_factory=list)
    computed_at: datetime | None = None
    frozen_at: datetime | None = None
    pixel_oss_key: str | None = None
    disclaimer_zh: str
    rule_zh: str


class EmergencePixel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    lon: float
    lat: float
    increment: float = Field(description="当日 NDVI 减去该像元土壤本底")
    pixel_class: int = Field(
        alias="class",
        serialization_alias="class",
        description="0 噪声内，1 高出但未持续，2 持续高出本底噪声",
    )
    baseline_ndvi: float
    ndvi: float


class AutumnEmergencePixelsOut(BaseModel):
    """像元增量，给现有 lon/lat 热力图用。不来自 s1s2_parcel NDVI JSON。"""

    land_id: str
    field_id: str
    season_key: str
    date: date
    as_of_date: date | None = None
    method_version: str
    noise_floor: float | None = None
    frozen: bool = False
    emerged_flag: bool = False
    status: str | None = None
    status_zh: str | None = None
    format: Literal["lonlat_v1"] = "lonlat_v1"
    value: Literal["increment"] = "increment"
    class_labels: dict[str, str]
    disclaimer_zh: str
    pixel_count: int
    counts: dict[str, int]
    pixels_lonlat: list[EmergencePixel]
    source: Literal["stored"] = "stored"


def class_labels() -> dict[str, str]:
    from app.core.autumn_emergence import CLASS_LABELS_ZH

    return dict(CLASS_LABELS_ZH)
