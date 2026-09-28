"""L7 统一配置层（权威：07 篇 §6——默认值 → OA_ 环境变量 → .env；密钥不落库）。

全平台唯一取配置入口：`from services.platform.config import get_settings`。
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

    # 插件市场两级签名平台私钥（Skills §5.1，Ed25519 raw seed hex 64 字符；M5-1 增项，报告已报）。
    # 缺失/非法 → 发布与安装验签 fail-closed（发布 4510 拒绝，安装 4510/4509 拒绝，禁静默降级）。
    # 开发期生成 dev key：
    #   python -c "from services.platform.security import generate_signing_key; print(generate_signing_key())"
    # 产出后写入 .env 的 OA_PLATFORM_PLUGIN_SIGNING_KEY；生产经部署密钥管理下发，禁入库。
    platform_plugin_signing_key: str | None = None

    # 模型网关（14 篇 §9：本地 vLLM 主力 + 云渠道溢出，均为 OpenAI 兼容端点。
    # M0 单渠道直连；多渠道 fallback 待 foundation/llm 落地收编进 model_channels）
    llm_base_url: str | None = None  # 云渠道 https://api.deepseek.com；本地 vLLM http://127.0.0.1:8001/v1
    llm_model: str = "deepseek-chat"  # 现役默认对话模型；本地渠道用 vLLM served-model-name 固定值 local-main
    llm_api_key: str | None = None
    # M0 历史字段（ops/03 单轨 Ollama 时期）：14 篇 §9 vLLM 定稿后随网关改造移除，勿新增依赖
    ollama_base_url: str = "http://localhost:11434"

    # 能力层 P0（docs/Agent/06）：fs 工作区根（None=禁用 fs 工具）与 web 出口白名单（空=全拒 fail-closed）
    workspace_root: str | None = None
    web_egress_allowlist: str = ""  # 逗号分隔域名；空=web 工具全拒

    # 网关运行
    api_prefix: str = "/api/v1"
    sse_heartbeat_seconds: int = 15  # 建议值，压测后冻结（02 §5）

    # 任务执行 worker（非 SSE 受理路径 + 重试监督；Agent 服务设计 §2 补全表，2026-09-27 批）
    task_worker_enabled: bool = True  # False=回到「queued 挂起等人工/外部消费」旧行为
    task_worker_poll_interval_s: float = 1.0  # 空转轮询间隔（OutboxRelay 同款 1s）
    task_spill_dir: str | None = None  # 工具结果 spill 存储目录（None=spill 关闭；M4 切 MinIO）

    # 记忆域（06 篇 §4/§5.2/§5.5；计划 1 仅 L1 与检索参数）
    memory_l1_ttl_seconds: int = 24 * 3600  # L1 会话记忆块 TTL（会话活跃期）
    memory_search_top_k: int = 8  # 检索注入条数上限
    memory_rrf_k: int = 60  # RRF 平滑常数（Σ1/(k+rank)）
    memory_decay_half_life_days: int = 30  # 衰减半衰期（天）

    # 沉淀与空闲调度（06 篇 §5.1/§5.5；M4 计划 2 落位 2026-09-28；沉淀模型复用上方 llm_model
    # ——注意现役默认 deepseek-chat 面向 OpenAI 兼容网关，沉淀 Ollama 通道需 OA_LLM_MODEL 指向本地模型）
    memory_l2_confidence_threshold: float = 0.65  # ≥ 阈值静默写，低于进待复核
    memory_observation_min_proof: int = 2  # 观察固化最少独立事实数
    memory_task_deadline_hours: int = 24  # 空闲任务 deadline，超期升级在线
    memory_vector_enabled: bool = False  # 向量投影开关（嵌入模型接入后开启；关闭=NullProjection 降级）
    llm_timeout_seconds: float = 60.0
    idle_off_peak_start_hour: int = 1  # 低峰直通时段 [start, end)
    idle_off_peak_end_hour: int = 7
    idle_gate_max_qps: int = 5  # 四信号阈值（全部达标才发令牌）
    idle_gate_max_queue_depth: int = 3
    idle_gate_max_llm_concurrency: int = 1
    idle_gate_max_active_sessions: int = 2

    # 知识库检索 ACL 预过滤开关（docs/OntRAG §4.3；M5 收缩裁决=13 篇「配置开关化（无迁移方案）」）。
    # 默认 false=零行为变化（存量三路 SQL 不变）；true 时按 documents.acl_tags 列存在性探测下推，
    # 列缺失自动 no-op 并 DEBUG 留痕。DDL（documents.acl_tags jsonb）为本批报告欠账（迁移唯一归属=并行 agent）。
    kb_acl_filter_enabled: bool = False

    # kb 术语对齐二级（嵌入余弦）阈值（OntRAG §2.4 步骤 4 起步值；层轴 M2.5 验收结论移植）。
    # 一级精确/包含未命中的候选名 × 种子类表层全量嵌入，余弦 ≥ 阈值即对齐；嵌入不可用整级跳过（降级不失败）。
    align_embed_threshold: float = Field(default=0.92, ge=0.0, le=1.0)

    # 在线忠实度抽检（docs/architecture/10 §2 缺口②，落点 08 §7.4）：对话完成路径按采样率
    # 抽中后记录 faithfulness 检查任务占位（LLM-as-judge 判定本体随评估批次接入）。
    # 默认开、采样率 1%（10 篇口径）；确定性采样（run_id 哈希桶，可复现可追溯）。
    faithfulness_sampling_enabled: bool = True
    faithfulness_sample_rate: float = Field(default=0.01, ge=0.0, le=1.0)

    # 沙箱运行时（模块 13；docs/Sandbox §7.2 容量核算 lite 档默认——实测后冻结）
    sandbox_network_name: str = "oa-sandbox"  # internal 网（默认全拒，PoC P2 实测）
    sandbox_snapshot_dir: str = "var/sandbox"  # 快照落盘根（v1 本地；MinIO 化后为桶前缀）
    sandbox_session_pool_size: int = 2  # session 预热池（Sandbox §5.2）
    sandbox_fncall_pool_size: int = 4  # 短执行池
    sandbox_hibernate_after_minutes: int = 30  # 空闲休眠阈值（联动 sessions.idle_timeout_at）
    sandbox_snapshot_retain_hours: int = 72  # 快照保留期（Sandbox §5.5）
    sandbox_max_active: int = 11  # 16G lite 档小档并发上限（含池；v1 不超卖）

    @property
    def pg_dsn(self) -> str:
        return f"postgresql+psycopg://{self.pg_user}:{self.pg_password}@{self.pg_host}:{self.pg_port}/{self.pg_db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
