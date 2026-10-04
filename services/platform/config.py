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

    # 模型韧性（M4.5-C，docs/Agent/12 §3 批次 C：凭证池 + 模型冷却/fallback 链 + 调用级重试落盘）。
    # 缺省全部零行为变化：extra 空串=单凭证直驱（不建池）、chains 空串=禁用降级链。
    llm_api_keys_extra: str = ""  # 逗号分隔附加 API key（并入主 provider 凭证池；主 key=llm_api_key）
    llm_credential_cooldown_s: int = Field(default=60, ge=1)  # 单凭证 429/401 首次冷却；连续失败 ×2 递增
    llm_credential_cooldown_max_s: int = Field(default=600, ge=1)  # 冷却递增封顶
    llm_model_fail_threshold: int = Field(default=3, ge=1)  # per-model 连续失败次数 ≥ 阈值 → 冷却
    llm_model_cooldown_s: int = Field(default=120, ge=1)  # 模型冷却时长（到期自愈，成功清零连败计数）
    llm_fallback_chains: str = ""  # 降级链 "main->backup;a->b->c"（->接续；分号分隔多链；空=禁用）
    llm_call_retry_max_attempts: int = Field(default=2, ge=1, le=10)  # 调用级总尝试数（含首次；1=不重试）
    llm_call_retry_backoff_ms: int = Field(default=200, ge=0)  # 退避基值（指数 ×2；先落 llm.retry_scheduled 再等）
    # M0 历史字段（ops/03 单轨 Ollama 时期）：14 篇 §9 vLLM 定稿后随网关改造移除，勿新增依赖
    ollama_base_url: str = "http://localhost:11434"
    # 嵌入端点协议开关（docs/Agent/09 §2.1 工程问题 2「嵌入协议漂移」）：ollama=POST
    # {base}/api/embed body {model,input}（默认，存量口径零变化）；tei=POST {base}/embed
    # body {inputs}，响应直接是数组的数组（huggingface TEI，本机 GPU 栈部署）。
    # TEI 部署示例：OA_OLLAMA_BASE_URL=http://127.0.0.1:18002 + OA_EMBED_PROTOCOL=tei
    embed_protocol: str = Field(default="ollama", pattern="^(ollama|tei)$")

    # 能力层 P0（docs/Agent/06）：fs 工作区根（None=禁用 fs 工具）与 web 出口白名单（空=全拒 fail-closed）
    workspace_root: str | None = None
    web_egress_allowlist: str = ""  # 逗号分隔域名；空=web 工具全拒

    # 网关运行
    api_prefix: str = "/api/v1"
    sse_heartbeat_seconds: int = 15  # 建议值，压测后冻结（02 §5）
    # 平台对外 base URL（32 篇 §一：链接 base=展示关注点归前端 location.origin 拼接；
    # 本字段为邮件邀约场景 M4 预留，M1 零消费——禁各模块直读 env，取值唯一入口=此处）
    platform_base_url: str | None = None

    # 任务执行 worker（非 SSE 受理路径 + 重试监督；Agent 服务设计 §2 补全表，2026-09-27 批）
    task_worker_enabled: bool = True  # False=回到「queued 挂起等人工/外部消费」旧行为
    task_worker_poll_interval_s: float = 1.0  # 空转轮询间隔（OutboxRelay 同款 1s）
    task_spill_dir: str | None = None  # 工具结果 spill 存储目录（None=spill 关闭；M4 切 MinIO）
    # H-0c ①（2026-09-29 批，评审行 19/20）：孤儿 running Run 超时回收——worker 进程崩溃后
    # running 悬挂无租约/心跳，靠 updated_at 悬挂时长兜底回收（租约制随多副本 D4）。
    task_orphan_sweep_interval_s: float = 30.0  # sweep 扫描周期（与轮询同进程常驻）
    task_orphan_running_timeout_s: float = 300.0  # running 悬挂判定阈值（对齐 hermes TTL 300s 口径）
    # H-0c ③：适配器 degraded 态阈值（04 §10 裁决：探活连续失败 N 次→degraded，
    # 新会话拒绑、存量 Run 跑完；成功清零自愈）
    agent_degrade_threshold: int = 3

    # B-① Run 内并行工具调度（docs/Agent/10 §3，T6 唯一事实源）：并行调度段内最大并发度；
    # =1 时所有段退化为单步=串行（零行为变化）。内核构造参数显式注入优先，缺省读此值（D2 纪律）。
    kernel_tool_parallelism: int = Field(default=4, ge=1, le=16)

    # H-2 上下文工程（2026-09-29 批，docs/Agent/07 边界契约 D-7/F-2/F-4 三层归属：
    # 策略=能力层/阈值=配置层/断点=内核）。context_budget_tokens=组装器绝对预算
    # （A1，原 kernel/grounding.py 的 GROUNDING_BUDGET_TOKENS 常量收编于此——内核不藏
    # 数值策略，D2 同款纪律）；context_compaction_threshold=压缩触发水位（估算 tokens
    # 超预算 × 阈值即触发，「压缩即再生成防线」，研究 07 §11；缺省 0.8=超预算 80% 触发）。
    context_budget_tokens: int = Field(default=4_000, gt=0)
    context_compaction_threshold: float = Field(default=0.8, ge=0.0, le=1.0)

    # M4.5-B token 锚定（docs/Agent/12 §2 批次 B）：锚定系数显著变化阈值——真实 usage
    # 到达校准 ratio=real_total/estimated_total 后，相对前值漂移超此比例才落
    # kernel.budget_anchor 事件（首锚恒落，小抖动静默不刷事件）。内核构造参数显式注入
    # 优先（BudgetTracker.anchor_drift_threshold），缺省运行期读此值（D2 纪律同上）。
    budget_anchor_drift_threshold: float = Field(default=0.2, gt=0.0, le=1.0)

    # 内核四维超时（B-③ 批，docs/Agent/10 §8.2）：原 kernel/loop.py 与 execution.py 的
    # 模块级常量（_TOOL_TIMEOUT_S/_PLANNING_TIMEOUT_S/_GATE_TIMEOUT_S/_SINK_TIMEOUT_S，
    # 30/10/1/5）收编于此——内核不藏数值策略，D2/F-4 同款纪律；默认值=原常量逐位一致。
    # 显式构造参数优先（测试与组合根直传通道），未传运行期读这里。
    kernel_tool_timeout_s: float = Field(default=30.0, gt=0)
    kernel_planning_timeout_s: float = Field(default=10.0, gt=0)
    kernel_gate_timeout_s: float = Field(default=1.0, gt=0)
    kernel_sink_timeout_s: float = Field(default=5.0, gt=0)

    # M4.5-A 运行中输入面（docs/Agent/12-M4.5运行中输入面与模型韧性设计（主仓本地）§1.1/§1.2）：
    # kernel_inbox_max_per_run=KernelInbox 每 Run 待处理条目容量上限（三队列合计；
    # 超限 4203 INBOX_CAPACITY 结构化拒绝）；estop_ttl_seconds=紧急停止键 TTL（§1.2 定稿 24h）。
    kernel_inbox_max_per_run: int = Field(default=8, ge=1, le=32)
    estop_ttl_seconds: int = Field(default=86_400, gt=0)
    # 子代理派发深度上限（40 篇 §8 R10，2026-10-04 批）：kernel spawn_sub 通道护栏的
    # 唯一权威取值——超限在 STARTED 之前结构化拒绝（不产生事件）。默认 2 与能力层
    # guards.MAX_DERIVATION_DEPTH_DEFAULT 对齐（能力层 ContextVar 护栏=第一线，本值=
    # kernel 通道的第二线；语义同源：根 Run 深度 0，允许派生当且仅当 子深度 ≤ 上限）。
    kernel_subagent_max_depth: int = Field(default=2, ge=0)

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

    # kb 文件直传通道（v1.5 wedge，feature/kb-file-parse）：multipart PDF 单文件字节上限；
    # 超限 413（错误码 3001 PARAM_INVALID，02 §7 既有段）。图纸 PDF 50MB 起步值，实测后冻结。
    kb_file_upload_max_bytes: int = Field(default=50 * 1024 * 1024, gt=0)

    # kb 解析引擎链（v1.5 wedge）：preprocess 对 MinIO 原件选引擎抽取文本。
    # pdfium=pypdfium2 文本层直抽（默认：零模型、确定性）；docling=懒加载可选引擎（重模型，
    # 不进主依赖——缺库自动降级 pdfium 并登记 meta.degraded=["parser"]；模型下载为一次性
    # 成本，国内镜像 HF_ENDPOINT=https://hf-mirror.com）；plumber=pdfplumber 兜底（懒加载
    # 可选，缺库同降级路径）。
    kb_parser: str = Field(default="pdfium", pattern="^(pdfium|docling|plumber)$")

    # kb nightly 例程（OntRAG §8.4 v1 收缩范围，KB-G1b）：预算三项的 v1 合并简化 + 调度互斥参数。
    # max_items_per_run = 单次运行硬上限（补嵌 chunk 数与归档动作数同源预算；超限顺延次夜记 stats.deferred）；
    # token_budget = 记录用（v1 不硬停：随 kb_maintenance_runs.stats 落账供成本对账，§8.4 纪律 1 的记账面）。
    kb_nightly_max_items_per_run: int = Field(default=5000, gt=0)
    kb_nightly_token_budget: int = Field(default=1_000_000, ge=0)
    kb_nightly_lock_ttl_seconds: int = Field(default=900, gt=0)  # Redis 锁 lock:kb_nightly TTL（心跳续期周期=TTL/3）
    # nightly 调度挂点（问题清单 A2；OntRAG §8.4 例程可运行化）：cron tick 每轮经
    # services/kb/business/nightly_schedule.py 判定达点即后台触发一次 run_nightly。
    # schedule_hour=2（每日 02:00，进程本地时区）为**示例值**，待运营按低峰窗口裁决调整；
    # enabled=false 整体摘除挂点（tick 侧短路零开销）。
    kb_nightly_schedule_enabled: bool = True
    kb_nightly_schedule_hour: int = Field(default=2, ge=0, le=23)

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
