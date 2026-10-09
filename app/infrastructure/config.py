from decimal import Decimal
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "neironych"
    app_env: str = "test"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite+aiosqlite:///./neironych.db"
    database_pool_size: int = Field(default=10, ge=1)
    database_max_overflow: int = Field(default=10, ge=0)
    database_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    database_pool_recycle_seconds: int = Field(default=1800, ge=1)
    redis_url: str = "redis://localhost:6379/0"
    admin_api_token: str = Field(default="change-me", min_length=1)
    admin_telegram_id: str | None = None
    telegram_bot_token: str | None = None
    terms_url: str | None = None
    privacy_policy_url: str | None = None
    legal_document_version: str = "2026-09-19"
    support_storage_dir: str = "./var/support"
    log_level: str = "INFO"
    rub_per_usdt: Decimal = Field(default=Decimal("100.00"), gt=0)
    seedance_25_edit_markup_rub_per_second: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    argolink_base_url: str = "https://argolink.io"
    argolink_api_key: str | None = None
    provider_notification_webhook_secret: SecretStr | None = None
    infai_system_token: SecretStr | None = None
    infai_user_id: int | None = Field(default=None, gt=0)
    provider_credentials_master_key: str | None = Field(default=None, min_length=32)
    opening_working_capital_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    required_provider_float_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    wallet_refresh_seconds: int = Field(default=60, ge=1)
    native_request_timeout_seconds: float = Field(default=600, gt=0)
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
    asale_base_url: str = "https://gw.asale.ai"
    asale_api_key: str | None = None
    asale_timeout_seconds: float = Field(default=30.0, gt=0)
    asale_http_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    asale_http_read_timeout_seconds: float = Field(default=30.0, gt=0)
    asale_http_write_timeout_seconds: float = Field(default=30.0, gt=0)
    asale_http_pool_timeout_seconds: float = Field(default=5.0, gt=0)
    asale_http_max_connections: int = Field(default=50, ge=1)
    asale_http_max_keepalive_connections: int = Field(default=25, ge=1)
    asale_http_keepalive_expiry_seconds: float = Field(default=30.0, gt=0)
    asale_submit_rps: float = Field(default=5.0, ge=0)
    asale_poll_rps: float = Field(default=20.0, ge=0)
    crypto_pay_api_token: str | None = None
    crypto_pay_base_url: str = "https://pay.crypt.bot"
    crypto_pay_timeout_seconds: float = Field(default=10.0, gt=0)
    payment_reconciliation_interval_seconds: float = Field(default=60.0, ge=5)
    payment_reconciliation_batch_size: int = Field(default=20, ge=1, le=100)
    public_api_base_url: str = "http://localhost:8000"
    public_media_base_url: str | None = None
    media_storage_backend: str = "local"
    media_local_storage_dir: str = "./var/media"
    media_retention_hours: int = 24
    media_share_link_ttl_seconds: int = Field(default=7 * 24 * 60 * 60, ge=300, le=30 * 24 * 60 * 60)
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
    worker_generation_max_retries: int = Field(default=2, ge=0, le=2)
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
