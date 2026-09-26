"""L7 统一配置层（权威：07 篇 §6——默认值 → OA_ 环境变量 → .env；密钥不落库）。

全平台唯一取配置入口：`from services.infra.config import get_settings`。
禁止各模块直读 os.getenv（standards/01 §2.8）。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OA_", env_file=".env", extra="ignore", frozen=True)

    # 部署档位（锚点 §3.2：lite 默认）。
    # 2026-09-26 缺口核查修复（别名裁决）：OA_DEPLOY_TIER 与 deploy_profile 二选一——
    # 保留 deploy_profile（OA_DEPLOY_PROFILE）为唯一权威；OA_DEPLOY_TIER 为废弃别名，
    # 不再映射任何字段（extra="ignore" 静默忽略），不得在新代码中引用。
    deploy_profile: str = Field(default="lite", pattern="^(lite|full)$")

    # 数据层（deploy/.env.example 对齐）
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "onto"
    pg_password: str = "onto_dev"
    pg_db: str = "onto"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "localhost:9000"
    # 2026-09-26 缺口核查修复：对齐 .env.example 的 OA_MINIO_USER/OA_MINIO_PASSWORD 与 compose MINIO_ROOT_*
    minio_user: str = "onto"
    minio_password: str = "onto_dev"

    # 安全（M1 启用；生产必填校验留 lifespan）
    jwt_secret: str = "dev-only-change-me"
    jwt_access_ttl_minutes: int = 120
    jwt_refresh_ttl_days: int = 14

    # 模型网关（ops/03 §2：单轨 Ollama 经 LiteLLM）
    ollama_base_url: str = "http://localhost:11434"
    llm_api_key: str | None = None

    # 网关运行
    api_prefix: str = "/api/v1"
    sse_heartbeat_seconds: int = 15  # 建议值，压测后冻结（02 §5）

    # 记忆域（06 篇 §4/§5.2/§5.5；计划 1 仅 L1 与检索参数）
    memory_l1_ttl_seconds: int = 24 * 3600      # L1 会话记忆块 TTL（会话活跃期）
    memory_search_top_k: int = 8                # 检索注入条数上限
    memory_rrf_k: int = 60                      # RRF 平滑常数（Σ1/(k+rank)）
    memory_decay_half_life_days: int = 30       # 衰减半衰期（天）

    @property
    def pg_dsn(self) -> str:
        return f"postgresql+psycopg://{self.pg_user}:{self.pg_password}@{self.pg_host}:{self.pg_port}/{self.pg_db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
