"""agric-satellite-analysis API - Core configuration."""

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# 容器/ABflow 往往只提供 .env 文件，不 export 到进程环境。
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True
    )

    # Database
    database_url: str = "postgresql+asyncpg://openfarm:openfarm_dev@db:5432/openfarm"
    database_url_sync: str = ""
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

    # Weather (Open-Meteo)
    open_meteo_forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_archive_url: str = "https://archive-api.open-meteo.com/v1/archive"
    open_meteo_api_key: str = ""
    weather_backfill_days: int = 365
    weather_batch_size: int = 50
    weather_gdd_base_temp: float = 10.0
    weather_heat_stress_threshold: float = 32.0

    # Index Backfill
    index_backfill_months: int = 60
    index_backfill_chunk_days: int = 90
    index_weekly_batch_size: int = 50

    # Soil Data
    soilgrids_wcs_base_url: str = "https://maps.isric.org/mapserv"
    polaris_s3_bucket: str = "polaris-soil-data"
    soil_fetch_timeout_seconds: int = 60
    soil_source_priority: str = "auto"  # auto | soilgrids | polaris

    # cdfinance / 中和农信 analyzeSoilV2 (NPK) + groupSiteAdmission.
    # Token is NEVER stored here — callers pass Bearer at request time.
    # App key is a public H5 header. Use joint-venture-test base for test.
    cdfinance_soil_base_url: str = "https://joint-venture.cdfinance.com.cn/agric-api"
    cdfinance_app_key: str = "83f94deg-k4d3-5gc4-0a6d-fd995a6f03g5"
    cdfinance_channel_net: str = "H5"
    cdfinance_hr_base_id: str = "38"
    cdfinance_origin: str = "https://joint-venture.cdfinance.com.cn"
    cdfinance_referer: str = "https://joint-venture.cdfinance.com.cn/"
    cdfinance_soil_timeout_seconds: int = 60

    # Email (Resend)
    resend_api_key: str = ""
    resend_from_email: str = "agric-satellite-analysis <noreply@openfarm.app>"
    app_url: str = "http://localhost:3000"

    # Internal download-host control plane (HTTP claim). Never expose to browsers.
    # WORK_QUEUE_MODE: legacy = MQ/Celery only; claim = work_items only;
    # dual = insert work_items AND keep MQ publish (transition).
    internal_api_token: str = ""
    work_queue_mode: str = "legacy"
    work_lease_seconds: int = 600
    work_claim_default_limit: int = 1
    work_reaper_on_claim: bool = True


settings = Settings()
