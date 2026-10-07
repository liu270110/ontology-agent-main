"""intent suite 可变参数（D2 纪律：一切可调参数走 Settings，禁散落常量/直读 env）。

- 基准自身参数（模型端点/采样/判拒阈值/并发）经本 Settings（env 前缀 ``BENCH_INTENT_``，
  可 .env 注入）；对齐 rag suite（suites/rag/config.py）同款形态；
- 指标口径常量（置信钳位、期望枚举等）归 metrics.py 口径字典，不在此重复。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class IntentBenchSettings(BaseSettings):
    """intent suite 运行参数（env 前缀 BENCH_INTENT_）。"""

    model_config = SettingsConfigDict(env_prefix="BENCH_INTENT_", env_file=".env", extra="ignore", frozen=True)

    # 结果标识
    tag: str = "v0"  # 运行标签（docs/Agent/16 §3：优化后换 tag 重跑，SUMMARY.md 留曲线）
    dataset_version: str = "v0"  # datasets/<version>.jsonl 金标集

    # 受评模型面（本地 vLLM，OpenAI 兼容 /v1/chat/completions；门禁：不用外网）
    vllm_base_url: str = "http://127.0.0.1:18001"
    vllm_model: str = "local-main"  # qwen3-4b-awq（/v1/models 实测 served id）
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)  # 0=贪心（可复现优先）
    max_tokens: int = Field(default=768, ge=1)  # qwen3 内联 <think> 占额，JSON 截断即 parse_error 如实计
    request_timeout_s: float = Field(default=120.0, gt=0)
    request_concurrency: int = Field(default=4, ge=1, le=32)  # 双档逐条并发（保序回填）

    # 判分口径
    out_of_scope_confidence_floor: float = Field(default=0.5, ge=0.0, le=1.0)  # 越界「映射最近+低置信」判拒下限

    # A2=jev 档（docs/Agent/17 §1 批次 A，红队 E1 闭环）：GLiNER 通道直调（不走 LLM）。
    # 引擎参数（threshold/timeout/model_id）复用平台 Settings（OA_JEV_*，D2 单一事实源）；
    # 本开关只控 bench 侧是否跑 A2 档（False=跳过，flat 指标三键如实置 None）。
    jev_tier_enabled: bool = True

    # 产物
    results_dir: Path = Path("benchmarks/results")  # 相对仓库根（run.py 以 cwd=仓库根运行）
    suite_name: str = "intent"
