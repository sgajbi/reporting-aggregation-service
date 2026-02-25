from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    contract_version: str = Field("v1", alias="CONTRACT_VERSION")
    pas_base_url: str = Field("http://localhost:8201", alias="PAS_BASE_URL")
    pa_base_url: str = Field("http://localhost:8002", alias="PA_BASE_URL")
    upstream_timeout_seconds: float = Field(10.0, alias="UPSTREAM_TIMEOUT_SECONDS")
    upstream_max_retries: int = Field(2, alias="UPSTREAM_MAX_RETRIES")
    upstream_retry_backoff_seconds: float = Field(0.2, alias="UPSTREAM_RETRY_BACKOFF_SECONDS")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
