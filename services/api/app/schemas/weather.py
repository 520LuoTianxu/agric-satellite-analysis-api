"""Pydantic schemas - weather data."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from agric_satellite_analysis_common.weather_window import (
    DEFAULT_WEATHER_BACKFILL_DAYS,
    MAX_WEATHER_HISTORY_DAYS,
    resolve_historical_weather_window,
)


# ── Weather Daily Record ─────────────────────────────────────────────


class WeatherDailyOut(BaseModel):
    date: date
    temperature_2m_min: float | None = None
    temperature_2m_max: float | None = None
    temperature_2m_mean: float | None = None
    precipitation_sum: float | None = None
    et0_fao_mm: float | None = None
    soil_temperature_0cm: float | None = None
    soil_temperature_6cm: float | None = None
    soil_temperature_18cm: float | None = None
    soil_temperature_54cm: float | None = None
    soil_moisture_0_1cm: float | None = None
    soil_moisture_1_3cm: float | None = None
    soil_moisture_3_9cm: float | None = None
    soil_moisture_9_27cm: float | None = None
    soil_moisture_27_81cm: float | None = None
    vapor_pressure_deficit: float | None = None
    shortwave_radiation_sum: float | None = None
    wind_speed_10m_max: float | None = None
    cloud_cover_mean: int | None = None
    gdd_daily: float | None = None
    gdd_cumulative: float | None = None
    water_balance_30d_mm: float | None = None
    drought_index: float | None = None
    heat_stress_flag: bool | None = None

    model_config = {"from_attributes": True}


class WeatherForecastDay(BaseModel):
    date: date
    temperature_2m_min: float | None = None
    temperature_2m_max: float | None = None
    temperature_2m_mean: float | None = None
    precipitation_sum: float | None = None
    et0_fao_mm: float | None = None
    wind_speed_10m_max: float | None = None
    cloud_cover_mean: int | None = None


# ── Weather Summary ──────────────────────────────────────────────────


class WeatherSummaryOut(BaseModel):
    land_id: str
    period_start: date
    period_end: date
    avg_temperature: float | None = None
    min_temperature: float | None = None
    max_temperature: float | None = None
    total_precipitation: float | None = None
    total_et0: float | None = None
    water_deficit_mm: float | None = None
    gdd_cumulative: float | None = None
    frost_days: int = 0
    heat_stress_days: int = 0
    avg_soil_moisture_top: float | None = None
    drought_index: float | None = None
    data_source: str = "open-meteo"
    last_updated: datetime | None = None


# ── Response Envelopes ───────────────────────────────────────────────


class WeatherLocation(BaseModel):
    latitude: float
    longitude: float


class WeatherResponse(BaseModel):
    land_id: str
    location: WeatherLocation
    data: list[WeatherDailyOut]
    forecast: list[WeatherForecastDay] = []
    summary: WeatherSummaryOut


class WeatherBackfillRequest(BaseModel):
    days: int = Field(
        DEFAULT_WEATHER_BACKFILL_DAYS,
        ge=1,
        le=MAX_WEATHER_HISTORY_DAYS,
    )
    years: int | None = Field(None, ge=1, le=10)
    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def validate_window(self):
        if self.date_from is not None and self.years is not None:
            raise ValueError("date_from and years cannot be used together")
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_to < self.date_from
        ):
            raise ValueError("date_to must be >= date_from")
        # 在 schema 层复用任务层的同一规则，提前拦截未来日期和超长窗口，
        # 避免请求已入队后才由下载机失败。
        resolve_historical_weather_window(
            days=self.days,
            years=self.years,
            date_from=self.date_from,
            date_to=self.date_to,
        )
        return self


class WeatherBackfillResponse(BaseModel):
    land_id: str
    status: str
    message: str
