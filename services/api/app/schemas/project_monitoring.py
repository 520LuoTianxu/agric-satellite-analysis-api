"""项目地图使用的轻量监测摘要，不传输影像像元和完整历史序列。"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

RiskLevel = Literal["high", "medium", "low", "normal", "unknown"]
DataStatus = Literal["fresh", "stale", "low_quality", "missing"]


class ProjectAlert(BaseModel):
    id: str
    date: date
    severity: str
    rule_name: str
    message: str
    status: str
    index_type: str | None = None


class ProjectObservation(BaseModel):
    date: date
    ndvi: float | None = None
    evi: float | None = None
    ndmi: float | None = None
    cloud_cover: float | None = None
    source: str | None = None


class ProjectLandMonitoring(BaseModel):
    land_id: str
    land_name: str | None = None
    area_mu: float | None = None
    crop_type: str | None = None
    boundary_geojson: dict[str, Any] | None = None
    risk_level: RiskLevel
    data_status: DataStatus
    latest_scene_date: date | None = None
    observation: ProjectObservation | None = None
    previous_observation: ProjectObservation | None = None
    open_alert_count: int = 0
    open_high_count: int = 0
    risk_alert_count: int = 0
    # 只展示优先级最高的几条依据；计数始终来自完整预警集合。
    alerts: list[ProjectAlert] = Field(default_factory=list)


class ProjectMonitoringOut(BaseModel):
    group_id: str
    as_of: date
    generated_at: datetime
    freshness_days: int
    items: list[ProjectLandMonitoring]
