from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from functools import cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False
    )

    snapshot_path: str = Field(default="dump.rdb")
    max_save_timeout: int = Field(default=3600)

@cache
def get_settings():
    return Settings()
