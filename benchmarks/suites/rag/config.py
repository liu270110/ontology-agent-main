"""rag suite 可变参数（D2 纪律：一切可调参数走 Settings，禁散落常量/直读 env）。

- 基准自身参数（k 值/阈值/端点/超时）经本 Settings（env 前缀 BENCH_RAG_，可 .env 注入）；
- 平台参数（嵌入端点等）仍归 services.platform.config.Settings：bench 仅在 ASGI 分支
  于进程启动前按 ``embed_base_url``/``embed_protocol`` 显式注入 OA_ 环境变量（None=不动
  平台配置），让「我们 kb 现检索」以真实部署形态运行；
- 指标口径常量（RRF k、分句规则等）归 metrics.py 口径字典，不在此重复。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RagBenchSettings(BaseSettings):
    """rag suite 运行参数（env 前缀 BENCH_RAG_）。"""

    model_config = SettingsConfigDict(env_prefix="BENCH_RAG_", env_file=".env", extra="ignore", frozen=True)

    # 结果标识
    tag: str = "v0"  # 运行标签（docs/Agent/16 §3：每次优化后换 tag 重跑，SUMMARY.md 留曲线）
    corpus_version: str = "v0"  # corpora/<version>/ 子目录

    # 检索与指标口径
    top_k: int = Field(default=8, ge=1, le=50)  # 双 harness 统一 top_k（/kb/search 同参）
    recall_k: int = Field(default=5, ge=1, le=50)  # recall@k 的 k（≤ top_k 才有意义）
    faithfulness_min_support: float = Field(default=0.5, ge=0.0, le=1.0)  # 句级 n-gram 支撑率阈值
    token_counter: Literal["vllm", "heuristic"] = "vllm"  # token 计数后端（vllm=/tokenize 真值）
    vllm_base_url: str = "http://127.0.0.1:18001"  # 本地 vLLM（token 计数 + 第二波 LLM judge）
    vllm_model: str = "local-main"

    # harness 面向（runner 消费）
    harness: Literal["auto", "http", "asgi"] = "auto"
    api_base: str = "http://127.0.0.1:8364/api/v1"  # http harness 的后端基地址
    request_timeout_s: float = Field(default=120.0, gt=0)
    ingest_timeout_s: float = Field(default=600.0, gt=0)  # 单文档流水线超时
    ingest_poll_interval_s: float = Field(default=0.5, gt=0)
    # ASGI 分支启动前注入的嵌入端点（None=不动平台配置；本机 TEI 形态见 platform/config 注释）
    embed_base_url: str | None = None
    embed_protocol: Literal["tei", "ollama"] | None = None  # None=不动平台配置

    # naive 基线（competitors/naive_rag.py）
    naive_embed_base_url: str = "http://127.0.0.1:18002"  # 简单向量基线用同一 bge-m3 端点（隔离策略差异）
    naive_embed_protocol: Literal["tei", "ollama"] = "tei"
    naive_embed_batch_size: int = Field(default=16, ge=1)
    bm25_k1: float = Field(default=1.5, gt=0)  # Okapi BM25 经典缺省（ Robertson & Zaragoza ）
    bm25_b: float = Field(default=0.75, ge=0.0, le=1.0)
    # naive 抽取式读者句数（口径对齐 ours 句级拼装上限）
    naive_answer_max_sentences: int = Field(default=3, ge=1, le=10)

    # 产物
    results_dir: Path = Path("benchmarks/results")  # 相对仓库根（run.py 以 cwd=仓库根运行）
    suite_name: str = "rag"
