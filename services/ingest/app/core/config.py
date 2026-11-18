"""agric-satellite-analysis ingest worker - Core configuration."""

from pydantic_settings import BaseSettings

from agric_satellite_analysis_common.weather_window import DEFAULT_WEATHER_BACKFILL_DAYS


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://openfarm:openfarm_dev@db:5432/openfarm"
    # 数据库只保留 agric_satellite；应用表、扩展和 Alembic 版本表均在此 schema。
    database_schema: str = "agric_satellite"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # JWT
    openfarm_jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = 3600  # 1 hour

    # Object storage backend: Aliyun OSS only.
    storage_backend: str = "oss"

    # Aliyun OSS (default primary backend)
    oss_region: str = "oss-cn-beijing"
    oss_endpoint: str = "https://oss-cn-beijing.aliyuncs.com"
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_bucket: str = "agric-dev"
    # Fixed product directory for S1/S2 parcel JSON
    oss_prefix: str = "s1s2_parcel/json/"

    # CORS
    cors_origins: str = "http://localhost:3000"

    # STAC
    stac_api_url: str = "https://earth-search.aws.element84.com/v1"

    # TiTiler
    titiler_internal_url: str = "http://tiler:80"
    titiler_public_url: str = "http://localhost:8080"

    # Weather (Open-Meteo primary; NASA POWER fallback)
    open_meteo_forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_archive_url: str = "https://archive-api.open-meteo.com/v1/archive"
    open_meteo_api_key: str = ""
    nasa_power_daily_url: str = (
        "https://power.larc.nasa.gov/api/temporal/daily/point"
    )
    weather_backfill_days: int = DEFAULT_WEATHER_BACKFILL_DAYS
    weather_batch_size: int = 50
    weather_gdd_base_temp: float = 10.0
    weather_heat_stress_threshold: float = 32.0

    # Index Backfill
    index_backfill_months: int = 60
    index_backfill_chunk_days: int = 90
    index_weekly_batch_size: int = 50
    # 每个下载机地块任务的检索/读取窗口边长；地块指标仍按原始边界掩膜。
    processing_window_km: float = 10.0

    # Soil Data
    soilgrids_wcs_base_url: str = "https://maps.isric.org/mapserv"
    polaris_s3_bucket: str = "polaris-soil-data"
    soil_fetch_timeout_seconds: int = 60
    # Concurrent SoilGrids WCS GetCoverage calls (props × depths × quantiles).
    soil_fetch_max_workers: int = 12
    # Concurrent Sentinel scene download+process threads inside one ingest job.
    # Bench (AWS windowed COG): workers=8 is the stable-fast sweet spot (~380
    # S2 scenes/min); 16 raises contention. Celery --concurrency is separate.
    ingest_scene_max_workers: int = 8
    # Rolling grace (seconds) after the last parallel scene completion before
    # abandoning still in-flight scenes so a stuck COG/HTTP call cannot block
    # batch finalize forever. Also applied from parallel start if zero scenes finish.
    ingest_scene_straggler_timeout_sec: float = 180.0
    # Process-wide cap on concurrent windowed band reads (GDAL/rasterio).
    # Nested under the scene pool; see app.core.band_parallel.
    ingest_band_max_workers: int = 8
    soil_source_priority: str = "auto"  # auto | soilgrids | polaris

    # Email (Resend)
    resend_api_key: str = ""
    resend_from_email: str = "agric-satellite-analysis <noreply@openfarm.app>"
    app_url: str = "http://localhost:3000"

    # Alibaba Bailian (season growth AI narrative)
    bailian_api_key: str = ""
    bailian_base_url: str = (
        "https://llm-7cudikcfvgf9l1hy.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    bailian_model: str = "qwen3.7flash"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()


def scene_max_workers() -> int:
    """Positive thread count for per-scene work inside one ingest job."""
    try:
        n = int(settings.ingest_scene_max_workers)
    except (TypeError, ValueError):
        n = 8
    return max(1, n)


def scene_straggler_timeout_sec() -> float:
    """Rolling seconds to wait after last scene completion before abandoning stragglers."""
    try:
        n = float(settings.ingest_scene_straggler_timeout_sec)
    except (TypeError, ValueError):
        n = 180.0
    return max(1.0, n)

# Windowed COG HTTP / slot knobs are env-driven (see app.core.cog_http) so tests
# can patch os.environ without constructing a new Settings instance. Helpers
# below re-export the canonical parsers.


def band_read_timeout_sec() -> float:
    """Parent wall and HTTP read/total timeout for one band attempt. Default 20s."""
    from app.core.cog_http import band_read_timeout_sec as _impl

    return _impl()


def band_read_connect_timeout_sec() -> float:
    from app.core.cog_http import band_read_connect_timeout_sec as _impl

    return _impl()


def band_read_hedge_after_sec() -> float:
    """Seconds before a second Range connection is started. Default 5s; 0 disables."""
    from app.core.cog_http import band_read_hedge_after_sec as _impl

    return _impl()


def gdal_slot_wait_sec() -> float:
    """Seconds to wait for a free windowed-read slot. Default 20s."""
    from app.core.cog_http import gdal_slot_wait_sec as _impl

    return _impl()


def band_scene_max_timeouts() -> int:
    """Skip remaining bands after this many timeouts in one scene. Default 2."""
    from app.core.cog_http import band_scene_max_timeouts as _impl

    return _impl()


def band_scene_budget_sec() -> float:
    """Skip remaining bands once in-slot IO for a scene has run this long. Default 60s."""
    from app.core.cog_http import band_scene_budget_sec as _impl

    return _impl()


def scene_max_inflight() -> int:
    """Process-wide cap on scenes doing windowed IO at once. Default 1."""
    from app.core.band_parallel import scene_max_inflight as _impl

    return _impl()

