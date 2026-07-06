from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    exa_api_key: str = ""
    gnews_api_key: str = ""
    database_url: str = "postgresql+psycopg2://stockmoves:stockmoves@localhost:5432/stockmoves"
    ingestion_interval_seconds: int = 6 * 60 * 60

    @field_validator("anthropic_model", mode="before")
    @classmethod
    def _default_if_blank(cls, v):
        # An explicitly-empty ANTHROPIC_MODEL (e.g. an unresolved 1Password
        # reference, or a blank value in .env) would otherwise override this
        # field's default with "", which the Anthropic API rejects outright.
        return v or DEFAULT_ANTHROPIC_MODEL


settings = Settings()
