"""eval release 评测可变参数（D2 纪律：一切可调参数走 Settings，禁散落常量/直读 env）。

env 前缀 ``BENCH_EVAL_``（pydantic-settings，可 .env 注入）。指标口径不在此——
口径唯一事实源=各 suite 的 metrics.py（17 篇 §2「指标口径复用各 suite metrics.py」），
本文件只管聚合器的行为参数（目录/文件名/趋势判定容差/可比性字段/期望场景数）。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 聚合的四个套件（17 篇 §2；目录名即 results/ 子目录名，含连字符为场景型）
KNOWN_SUITES: tuple[str, ...] = ("agent-core", "rag", "intent", "ontology-scale")

# 环境指纹透传键（各 run JSON env/manifest.env 的白名单；脱敏纪律：不含密钥类字段）
_ENV_FINGERPRINT_KEYS: tuple[str, ...] = (
    "commit",
    "branch",
    "dirty",
    "python",
    "platform",
    "deploy_profile",
    "model",
    "pyshacl",
    "rdflib",
    "token_counter",
    "vllm_model",
    "seed",
    "corpus_version",
    "corpus_sha256",
)


class EvalReleaseSettings(BaseSettings):
    """eval release 聚合评测参数（env 前缀 BENCH_EVAL_）。"""

    model_config = SettingsConfigDict(env_prefix="BENCH_EVAL_", env_file=".env", extra="ignore", frozen=True)

    # ---- 路径（相对仓库根；run.py 以 cwd=仓库根运行，与 RagBenchSettings.results_dir 同约定）----
    results_dir: Path = Path("benchmarks/results")
    citations_path: Path = Path("benchmarks/suites/rag/leaderboard_citations/public_leaderboards.json")

    # ---- 聚合范围 ----
    suites: tuple[str, ...] = KNOWN_SUITES
    tag: str = "v0"  # release 标签（CLI --tag 覆盖；17 篇 §4 首 curve 例=v0.2.0-m4.7）

    # ---- 产物文件名（results/eval/ 下：最新件 + <date>/ 历史件 + SUMMARY.md 追加曲线）----
    dashboard_filename: str = "dashboard.json"
    version_diff_filename: str = "version_diff.json"
    summary_filename: str = "SUMMARY.md"

    # ---- 趋势判定（diff 纯函数参数；↑/↓/→）----
    trend_flat_epsilon: float = Field(default=1e-9, ge=0.0)  # |delta|≤eps 视为平（→）
    delta_round_digits: int = Field(default=6, ge=0, le=12)  # delta 与比率值的统一精度（suite 同款）

    # ---- 完整性参照：场景型套件一次完整 run 的场景数（manifest 缺失时判 partial 用）----
    expected_scenario_counts: dict[str, int] = Field(default_factory=lambda: {"agent-core": 6, "ontology-scale": 3})

    # ---- 无 manifest 分组的 tag 回填：SUMMARY.md 时间戳最近邻回看窗口（秒）----
    tagless_summary_max_lookback_s: float = Field(default=6.0 * 3600.0, gt=0.0)

    # ---- 可比性指纹字段（diff 自动配对时两 run 必须同指纹才可比；否则 no_baseline）----
    #   single 型取 payload 顶层字段；scenario 型取装载后 run 的合成字段（smoke/scenarios）。
    comparability_fields: dict[str, tuple[str, ...]] = Field(
        default_factory=lambda: {
            "rag": ("config",),  # top_k/recall_k/queries 等跑法参数
            "intent": ("config", "dataset"),  # items 数/金标 sha256（探针 2 条 vs 金标 100 条不可比）
            "agent-core": ("smoke", "scenarios"),  # 冒烟档+逐场景参数（含并发/轮数）
            "ontology-scale": ("smoke", "scenarios"),  # 冒烟档+逐档参数（档位/预算）
        }
    )

    def results_root(self) -> Path:
        return self.results_dir

    def eval_dir(self) -> Path:
        return self.results_dir / "eval"

    def suite_dir(self, suite: str) -> Path:
        return self.results_dir / suite

    def env_fingerprint_keys(self) -> tuple[str, ...]:
        return _ENV_FINGERPRINT_KEYS
