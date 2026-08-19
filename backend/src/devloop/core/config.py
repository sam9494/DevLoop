from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DEVLOOP_", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+psycopg://devloop:devloop@localhost:5433/devloop"

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    # 憑證加密金鑰。空的話任何憑證操作都會直接失敗，不會退化成明文
    secret_key: str = ""

    # claude 子行程只能在這個目錄底下動作 —— 沒有 --cwd 旗標，靠 spawn cwd 限制
    workspace_root: Path = Path.home()
    claude_bin: str = "claude"
    claude_timeout_s: int = 900

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
