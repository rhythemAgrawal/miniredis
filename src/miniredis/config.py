from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from functools import cache
from typing import Literal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False
    )

    snapshot_path: str = Field(default="dump.rdb")
    snapshot_interval: int = Field(default=3600)
    max_save_timeout: int = Field(default=3600)
    buffer_drain_timeout: int = Field(default=120)
    fsync_policy: Literal["NO", "EVERYSEC", "ALWAYS"] = "EVERYSEC"
    aof_main_file_path: str = Field(default="main.aof")
    aof_temp_file_path: str = Field(default="temp.aof")

@cache
def get_settings():
    return Settings()
