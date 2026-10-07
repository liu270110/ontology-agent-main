"""ontology-scale suite 可变参数（D2 纪律：一切可调参数走 Settings，禁散落常量/直读 env）。

env 前缀 BENCH_ONTO_SCALE_（可 .env 注入）；指标口径常量归 metrics.py 口径字典。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OntoScaleBenchSettings(BaseSettings):
    """ontology-scale suite 运行参数（env 前缀 BENCH_ONTO_SCALE_）。"""

    model_config = SettingsConfigDict(env_prefix="BENCH_ONTO_SCALE_", env_file=".env", extra="ignore", frozen=True)

    # 规模梯度与确定性（generator.generate 同名参数的配置面）
    seed: int = 20261007
    instance_multiplier: int = Field(default=10, ge=1, le=1000)  # 每类实例数（实例数=10N 缺省）
    violation_ratio: float = Field(default=0.01, ge=0.0, lt=1.0)  # 故意违例实例占比

    # validate_latency 采样（含/不含违例两形态 × repeats 次取分位）
    latency_repeats: int = Field(default=3, ge=1, le=100)

    # F3 变异探针（mutation_targets 三契约型+TBox-only 对照，数量=每类靶规模下固定 4 处）
    reindex_mutations: int = Field(default=4, ge=3, le=4)  # 3=只跑契约三型；4=含 TBox-only 对照

    # 10⁴ 档墙钟预算（生成+三指标；超时如实记 partial，不伪造完成）
    tier_timeout_s: float = Field(default=900.0, gt=0)

    # token 计数后端（rag suite 同款双档：vllm=本地 /tokenize 真分词器；heuristic=确定性估算）
    token_counter: Literal["vllm", "heuristic"] = "vllm"
    vllm_base_url: str = "http://127.0.0.1:18001"  # 本地 vLLM（qwen3-4b-awq，model id=local-main）
    vllm_model: str = "local-main"

    # 规模档（可裁剪重跑单档；缺省全梯度）
    tiers: list[int] = Field(default_factory=lambda: [100, 1000, 10000])

    # 产物
    results_dir: str = "benchmarks/results"  # 相对仓库根（run.py 以 cwd=仓库根运行）
    suite_name: str = "ontology-scale"
