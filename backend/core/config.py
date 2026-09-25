"""运行时配置。所有值来自环境变量(本地通过 .env 注入)。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    app_secret_key: str = "change-me"
    api_base_url: str = "http://localhost:8000"

    database_url: str = "postgresql+asyncpg://growth:growth@localhost:5432/growth_agent"
    redis_url: str = "redis://localhost:6379/0"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    agent_max_tokens: int = 4096

    @property
    def cors_origins(self) -> list[str]:
        if self.app_env == "production":
            return [self.api_base_url]
        return ["http://localhost:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
