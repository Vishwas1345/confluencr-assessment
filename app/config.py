from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    log_level: str = "INFO"
    port: int = 8000

    database_url: str
    test_database_url: str | None = None
    redis_url: str

    processing_delay_seconds: float = Field(default=30.0, gt=0)
    processing_lease_seconds: int = Field(default=120, gt=0)
    dispatch_stale_seconds: int = Field(default=120, gt=0)
    reconcile_interval_seconds: int = Field(default=5, gt=0)
    worker_concurrency: int = Field(default=20, gt=0)

    @field_validator("database_url", "test_database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: object) -> object:
        if isinstance(value, str) and value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_lease(self) -> "Settings":
        if self.processing_lease_seconds <= self.processing_delay_seconds:
            raise ValueError(
                "PROCESSING_LEASE_SECONDS must be greater than PROCESSING_DELAY_SECONDS"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
