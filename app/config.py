from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"
    newsapi_api_key: str = ""
    gnews_api_key: str = ""
    database_url: str = "postgresql+psycopg2://stockmoves:stockmoves@localhost:5432/stockmoves"
    ingestion_interval_seconds: int = 6 * 60 * 60


settings = Settings()
