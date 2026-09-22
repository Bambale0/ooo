from decimal import Decimal
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "neironych"
    app_env: str = "test"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite+aiosqlite:///./neironych.db"
    redis_url: str = "redis://localhost:6379/0"
    admin_api_token: str = Field(default="change-me", min_length=1)
    telegram_bot_token: str | None = None
    log_level: str = "INFO"
    rub_per_usdt: Decimal = Decimal("100.00")
    argolink_base_url: str = "https://argolink.io"
    argolink_api_key: str | None = None
    provider_credentials_master_key: str | None = Field(default=None, min_length=32)
    argolink_timeout_seconds: float = 30.0
    argolink_http_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    argolink_http_read_timeout_seconds: float = Field(default=30.0, gt=0)
    argolink_http_write_timeout_seconds: float = Field(default=30.0, gt=0)
    argolink_http_pool_timeout_seconds: float = Field(default=5.0, gt=0)
    argolink_http_max_connections: int = Field(default=100, ge=1)
    argolink_http_max_keepalive_connections: int = Field(default=50, ge=1)
    argolink_http_keepalive_expiry_seconds: float = Field(default=30.0, gt=0)
    argolink_submit_rps: float = Field(default=10.0, ge=0)
    argolink_poll_rps: float = Field(default=50.0, ge=0)
    public_api_base_url: str = "http://localhost:8000"
    public_media_base_url: str | None = None
    media_storage_backend: str = "local"
    media_local_storage_dir: str = "./var/media"
    media_retention_hours: int = 24
    media_max_download_bytes: int = 500 * 1024 * 1024
    s3_endpoint_url: str | None = None
    s3_region_name: str = "auto"
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    worker_poll_interval_seconds: float = 5.0
    worker_batch_size: int = 20
    worker_submit_concurrency: int = Field(default=10, ge=1)
    worker_poll_concurrency: int = Field(default=10, ge=1)
    worker_initial_poll_delay_seconds: float = Field(default=5.0, gt=0)
    worker_poll_backoff_base_seconds: float = Field(default=5.0, gt=0)
    worker_poll_backoff_max_seconds: float = Field(default=30.0, gt=0)
    worker_max_retries: int = 3
    worker_retry_base_seconds: float = 5.0
    worker_retry_max_seconds: float = 300.0
    worker_provider_processing_timeout_seconds: float = 30 * 60
    worker_shutdown_grace_seconds: float = 25.0
    webhook_timeout_seconds: float = Field(default=10.0, gt=0)
    webhook_retry_interval_seconds: float = Field(default=15 * 60, gt=0)
    webhook_retry_window_seconds: float = Field(default=24 * 60 * 60, gt=0)
    webhook_claim_lease_seconds: float = Field(default=60.0, gt=0)
    webhook_worker_batch_size: int = Field(default=50, ge=1)
    webhook_worker_concurrency: int = Field(default=10, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
