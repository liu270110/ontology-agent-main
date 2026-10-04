#!/usr/bin/env python3
"""PoC4 L2 记忆引擎选型 harness（实施计划 13 篇 §3.4；裁决依据 docs/memory/多层记忆设计.md §1 裁决框 / §8）。

两路证据，输出 services/devtools/poc4_results.json（全程可追溯，回报告 docs/architecture/PoC④-记忆引擎选型报告.md）：

- mem0 实测（live）：mem0ai 1.0.5 + 本地 vLLM（OpenAI 兼容，qwen3-4b-awq）+ 本地 TEI bge-m3 嵌入
  + PG pgvector 向量库。电力域自造语料 22 条基础事实 + 4 条后续更新/否定写入（探测 ADD/UPDATE
  冲突语义，对应设计 §2 动作表）→ 逐条写入延迟；20 条检索 query（gold 事实 + 关键词备选）→
  hit@1/3/5、MRR、陈旧命中（更新后旧事实仍被召回 = 冲突处理未收敛信号）；PG 表 + history 库存储占用。
- Graphiti 结构化评估（未实测运行时，显著标注）：pip download --no-deps 取官方 wheel METADATA
  解析依赖树 + pip install --dry-run 做本机解析冲突检查；Neo4j 社区版兼容（无多 database/RBAC）、
  资源画像、与 lite 档冲突为文档级结构化结论（设计 §1 裁决框要求的兼容性确认）。

命中判定双规则（自评口径，规则确定性可复算）：主=payload.poc_fact_id ∈ gold 事实集；
备=归一化文本包含某 gold 关键词组全部成员（NFKC+去空白+小写）。更新探针另记 stale 命中。

用法：
  python services/devtools/poc4_memory_engine.py                    # 双路全跑（mem0 live + Graphiti 评估）
  python services/devtools/poc4_memory_engine.py --skip-mem0        # 只跑 Graphiti 评估
  python services/devtools/poc4_memory_engine.py --no-reset         # 不清空 collection（追加口径）
  python services/devtools/poc4_memory_engine.py --limit 3          # mem0 只跑前 3 条写入（管线自检）

依赖：pip install mem0ai（1.0.5）；pgvector 容器（本机 = oa-poc-pgvector:5433，PoC 专用临时容器，
主栈 postgres:16-alpine 未带 vector 扩展——该事实本身入报告「存储依赖」维度）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Final

os.environ.setdefault("MEM0_TELEMETRY", "False")  # posthog 遥测关闭（本地 PoC 不外发）

REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUTPUT: Final = REPO_ROOT / "services" / "tools" / "poc4_results.json"
MB: Final = 1024 * 1024
SEARCH_TOP_K: Final = 5
GRAPHITI_PKG: Final = "graphiti-core"

# ---------------------------------------------------------------------------
# 自造电力域语料（写入顺序即冲突探测时序；gold 关键词组允许多备选，兼容引擎改写/合并）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FactWrite:
    """一条写入语料：fact_id / 文本 / category（对齐 memory_l2_facts.category 枚举口径）。"""

    fact_id: str
    text: str
    category: str  # profile | preference | fact | event


BASE_FACTS: Final[tuple[FactWrite, ...]] = (
    FactWrite("f01", "用户张伟负责10kV滨河线的运维巡视工作。", "profile"),
    FactWrite("f02", "滨河线153开关是滨河线东段的主开关。", "fact"),
    FactWrite("f03", "滨河线153开关过流保护定值为400A。", "fact"),
    FactWrite("f04", "城东1号配变容量400kVA，位于滨河线东段。", "fact"),
    FactWrite("f05", "城东1号配变向实验中学供电。", "fact"),
    FactWrite("f06", "张伟希望故障工单的派单通知走短信而不是电话。", "preference"),
    FactWrite("f07", "张伟习惯在日报里先看跳闸次数再看停电时户数。", "preference"),
    FactWrite("f08", "抢修一班负责人是李卫东。", "fact"),
    FactWrite("f09", "抢修二班负责人是张建国。", "fact"),
    FactWrite("f10", "2026年3月2日滨河线153开关过流保护动作跳闸，滨河线全线失压。", "event"),
    FactWrite("f11", "3月2日滨河线故障停运4.5小时，影响滨河线东段全部用户。", "event"),
    FactWrite("f12", "人民医院由滨河线东段供电，是双电源重要用户。", "fact"),
    FactWrite("f13", "纺织厂由开发区线工业区段供电。", "fact"),
    FactWrite("f14", "10kV朝阳线312开关是大风易跳闸点，每年大风季节需要特巡。", "fact"),
    FactWrite("f15", "张伟的值班班组是检修甲班，每两周轮换一次。", "fact"),
    FactWrite("f16", "滨河线东段在2025年完成了双回路改造。", "fact"),
    FactWrite("f17", "故障工单GD-2026-0301派给抢修一班处理滨河线东段故障。", "event"),
    FactWrite("f18", "操作票P-2026-0042由李卫东执行。", "fact"),
    FactWrite("f19", "朝阳线北段雷雨季节故障率偏高，建议加装避雷器。", "fact"),
    FactWrite("f20", "南郊线目前状态正常，可以承接转供负荷。", "fact"),
    FactWrite("f21", "开发区2号配变容量630kVA，接于开发区线工业区段。", "fact"),
    FactWrite("f22", "张伟的手机尾号8842，紧急情况联系用。", "profile"),
)

# 后续写入：UPDATE/否定语义探针（对应设计 §2 ADD/UPDATE/DELETE 动作表；时序在基础事实之后）
FOLLOWUP_FACTS: Final[tuple[FactWrite, ...]] = (
    FactWrite("u01", "3月2日滨河线故障已全部处理完毕，滨河线现已全线恢复正常运行。", "event"),
    FactWrite("u02", "抢修一班负责人已变更为王志强。", "fact"),
    FactWrite("u03", "张伟不再要求短信通知，故障工单派单改为电话通知。", "preference"),
    FactWrite("u04", "滨河线东段双回路改造已于2026年3月通过验收。", "fact"),
)

ALL_WRITES: Final[tuple[FactWrite, ...]] = BASE_FACTS + FOLLOWUP_FACTS


@dataclass(frozen=True, slots=True)
class RecallQuery:
    """一条检索 query：gold 事实集（payload 主规则）+ 关键词组备选（文本兜底）+ 陈旧探针可选。"""

    query_id: str
    query: str
    gold_fact_ids: frozenset[str]
    gold_keyword_sets: tuple[tuple[str, ...], ...]
    stale_keywords: tuple[str, ...] = ()  # 非空：命中该组关键词=陈旧命中（更新未收敛信号）


RECALL_QUERIES: Final[tuple[RecallQuery, ...]] = (
    RecallQuery("q01", "滨河线的运维巡视是谁负责", frozenset({"f01"}), (("张伟", "滨河线"),)),
    RecallQuery("q02", "城东1号配变给哪些用户供电", frozenset({"f05"}), (("城东1号配变", "实验中学"),)),
    RecallQuery(
        "q03",
        "3月2日滨河线发生了什么，现在状态如何",
        frozenset({"f10", "u01"}),
        (("滨河线", "过流保护"), ("滨河线", "恢复正常")),
    ),
    RecallQuery("q04", "现在抢修一班的负责人是谁", frozenset({"f08", "u02"}), (("王志强",),), ("李卫东",)),
    RecallQuery(
        "q05",
        "张伟要怎么接收故障工单派单通知",
        frozenset({"f06", "u03"}),
        (("张伟", "电话"), ("张伟", "短信")),
    ),
    RecallQuery("q06", "滨河线东段双回路改造进展如何", frozenset({"f16", "u04"}), (("双回路",),)),
    RecallQuery("q07", "人民医院的供电电源来自哪里", frozenset({"f12"}), (("人民医院", "滨河线东段"),)),
    RecallQuery("q08", "纺织厂由哪条线路供电", frozenset({"f13"}), (("纺织厂", "开发区线"),)),
    RecallQuery("q09", "朝阳线312开关大风天要注意什么", frozenset({"f14"}), (("312开关",),)),
    RecallQuery("q10", "朝阳线北段防雷有什么建议", frozenset({"f19"}), (("避雷器",),)),
    RecallQuery("q11", "城东1号配变容量是多少", frozenset({"f04"}), (("城东1号配变", "400kVA"),)),
    RecallQuery("q12", "开发区2号配变的基本参数", frozenset({"f21"}), (("开发区2号配变", "630kVA"),)),
    RecallQuery("q13", "抢修二班由谁负责", frozenset({"f09"}), (("张建国",),)),
    RecallQuery("q14", "工单GD-2026-0301的处理情况", frozenset({"f17"}), (("GD-2026-0301",),)),
    RecallQuery("q15", "操作票P-2026-0042是谁执行的", frozenset({"f18"}), (("P-2026-0042",),)),
    RecallQuery("q16", "张伟在哪个班组值班", frozenset({"f15"}), (("检修甲班",),)),
    RecallQuery("q17", "滨河线东段有哪些重要用户", frozenset({"f12"}), (("人民医院",),)),
    RecallQuery("q18", "南郊线现在能转供负荷吗", frozenset({"f20"}), (("南郊线", "正常"),)),
    RecallQuery("q19", "153开关的保护定值是多少", frozenset({"f03"}), (("400A",),)),
    RecallQuery("q20", "张伟看日报有什么习惯", frozenset({"f07"}), (("日报",),)),
)

_WS: Final = re.compile(r"\s+")


def norm_text(text: str) -> str:
    """归一化：NFKC + 去全部空白 + 小写（关键词包含判定的确定性口径）。"""
    return _WS.sub("", unicodedata.normalize("NFKC", text)).lower()


# ---------------------------------------------------------------------------
# 配置解析：默认取 .env（OA_LLM_*），可用参数覆盖；tools 零平台依赖（对齐 poc2 惯例）
# ---------------------------------------------------------------------------


def load_env_values() -> dict[str, str]:
    """读仓库根 .env（跳过注释与空行，仅 KEY=VALUE），供 LLM/PG 缺省值来源。"""
    values: dict[str, str] = {}
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    return values


@dataclass(frozen=True, slots=True)
class PoCConfig:
    """PoC4 运行配置（sanitized 后可入结果 JSON；api_key 不落盘）。"""

    llm_base_url: str
    llm_model: str
    embed_base_url: str
    embed_model: str
    embed_dims: int
    pg_host: str
    pg_port: int
    pg_db: str
    pg_user: str
    collection: str
    user_id: str

    def sanitized(self) -> dict[str, str | int]:
        """可落盘视图（无密钥字段）。"""
        return {
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "embed_base_url": self.embed_base_url,
            "embed_model": self.embed_model,
            "embed_dims": self.embed_dims,
            "pg_host": self.pg_host,
            "pg_port": self.pg_port,
            "pg_db": self.pg_db,
            "pg_user": self.pg_user,
            "collection": self.collection,
            "user_id": self.user_id,
        }


def build_config(args: argparse.Namespace) -> PoCConfig:
    """参数 > .env > 内置缺省（内置缺省=本机实测拓扑：vLLM 18001 / TEI 18002 / PoC pgvector 5433）。"""
    env = load_env_values()
    return PoCConfig(
        llm_base_url=args.llm_base_url or env.get("OA_LLM_BASE_URL", "http://127.0.0.1:18001/v1"),
        llm_model=args.llm_model or env.get("OA_LLM_MODEL", "local-main"),
        embed_base_url=args.embed_base_url,
        embed_model=args.embed_model,
        embed_dims=args.embed_dims,
        pg_host=args.pg_host,
        pg_port=args.pg_port,
        pg_db=args.pg_db,
        pg_user=args.pg_user,
        collection=args.collection,
        user_id="poc4-user",
    )


# ---------------------------------------------------------------------------
# mem0 实测路（live）
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WriteRecord:
    """单次写入记录：耗时 + mem0 判定事件（ADD/UPDATE/DELETE/NONE 计数）+ 产出记忆文本。"""

    fact_id: str
    seconds: float
    events: dict[str, int] = field(default_factory=dict)
    memories: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RecallRecord:
    """单条检索记录：耗时 / 命中规则 / 排名 / 陈旧命中与 top 文本（可追溯）。"""

    query_id: str
    query: str
    seconds: float
    hit: bool
    hit_rank: int  # 1-based；未命中为 0
    rule: str  # fact_id | keywords | none
    stale_hit: bool
    top_texts: list[str] = field(default_factory=list)


def drop_collection_if_exists(cfg: PoCConfig, password: str) -> str:
    """清空 collection 表（幂等重跑基线）；返回动作说明（dropped / skipped）。"""
    import psycopg2

    conn = psycopg2.connect(
        host=cfg.pg_host, port=cfg.pg_port, dbname=cfg.pg_db, user=cfg.pg_user, password=password
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{cfg.collection}"')  # 标识符来自本地参数（受控），非外部输入
        conn.commit()
    finally:
        conn.close()
    return "dropped"


def run_mem0(cfg: PoCConfig, password: str, *, reset: bool, limit: int | None) -> dict[str, object]:
    """mem0 live 基准：写入（含冲突探针）→ 检索（hit/MRR/陈旧）→ 存储占用。初始化失败返回 error 记录。"""
    try:
        from mem0 import Memory
    except ImportError as exc:
        return {"mode": "live", "engine": "mem0", "measured": False, "error": f"mem0ai import failed: {exc}"}

    history_path = Path(tempfile.gettempdir()) / "poc4_mem0_history.db"
    if history_path.exists():
        history_path.unlink()
    reset_action = drop_collection_if_exists(cfg, password) if reset else "skipped(--no-reset)"
    config: dict[str, object] = {
        "llm": {
            "provider": "openai",
            "config": {
                "model": cfg.llm_model,
                "openai_base_url": cfg.llm_base_url,
                "api_key": "EMPTY",
                "temperature": 0.0,
                "max_tokens": 2000,
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": cfg.embed_model,
                "openai_base_url": cfg.embed_base_url,
                "api_key": "EMPTY",
                "embedding_dims": cfg.embed_dims,
            },
        },
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "dbname": cfg.pg_db,
                "collection_name": cfg.collection,
                "embedding_model_dims": cfg.embed_dims,
                "host": cfg.pg_host,
                "port": cfg.pg_port,
                "user": cfg.pg_user,
                "password": password,
            },
        },
        "history_db_path": str(history_path),
        "disable_history": False,
    }
    try:
        memory = Memory.from_config(config)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — 初始化失败要进结果 JSON 而非中断 Graphiti 路
        return {
            "mode": "live",
            "engine": "mem0",
            "measured": False,
            "error": f"Memory.from_config failed: {type(exc).__name__}: {exc}",
        }

    # -- 写入（时序即冲突探测时序：基础 22 条 → 更新/否定 4 条；collection 表由 from_config 重建） --
    writes: list[WriteRecord] = []
    facts = ALL_WRITES[:limit] if limit else ALL_WRITES
    for fact in facts:
        meta = {"category": fact.category, "poc_fact_id": fact.fact_id}
        started = time.perf_counter()
        result = memory.add(fact.text, user_id=cfg.user_id, metadata=meta)
        elapsed = time.perf_counter() - started
        events: dict[str, int] = {}
        texts: list[str] = []
        for item in result.get("results", []):
            event = str(item.get("event", "?"))
            events[event] = events.get(event, 0) + 1
            if item.get("memory"):
                texts.append(str(item["memory"]))
        writes.append(WriteRecord(fact.fact_id, round(elapsed, 3), events, texts))
        print(f"  [mem0 add] {fact.fact_id}: {elapsed:.2f}s events={events}", flush=True)

    # -- 检索（gold 双规则命中 + 陈旧探针） --------------------------------------
    recalls: list[RecallRecord] = []
    for rec in RECALL_QUERIES:
        started = time.perf_counter()
        response = memory.search(rec.query, user_id=cfg.user_id, limit=SEARCH_TOP_K)
        elapsed = time.perf_counter() - started
        results = response.get("results", [])
        top_texts = [str(r.get("memory", "")) for r in results]
        hit, rank, rule = False, 0, "none"
        for idx, item in enumerate(results, start=1):
            payload = item.get("metadata") or {}
            fact_link = str(payload.get("poc_fact_id", ""))
            text_n = norm_text(str(item.get("memory", "")))
            if fact_link and fact_link in rec.gold_fact_ids:
                hit, rank, rule = True, idx, "fact_id"
                break
            if any(all(k in text_n for k in keys) for keys in rec.gold_keyword_sets):
                hit, rank, rule = True, idx, "keywords"
                break
        stale = bool(rec.stale_keywords) and any(
            all(k in norm_text(t) for k in rec.stale_keywords) for t in top_texts
        )
        recalls.append(
            RecallRecord(rec.query_id, rec.query, round(elapsed, 4), hit, rank, rule, stale, top_texts[:SEARCH_TOP_K])
        )
        print(f"  [mem0 search] {rec.query_id}: hit={hit} rank={rank} rule={rule} stale={stale}", flush=True)

    # -- 汇总与存储占用 ----------------------------------------------------------
    all_memories = memory.get_all(user_id=cfg.user_id)
    stored = all_memories.get("results", []) if isinstance(all_memories, dict) else []
    write_seconds = [w.seconds for w in writes]
    search_seconds = [r.seconds for r in recalls]
    result_payload: dict[str, object] = {
        "mode": "live",
        "engine": "mem0",
        "measured": True,
        "package_version": metadata.version("mem0ai"),
        "reset_action": reset_action,
        "config": cfg.sanitized(),
        "n_writes": len(writes),
        "writes": [
            {"fact_id": w.fact_id, "seconds": w.seconds, "events": w.events, "memories": w.memories}
            for w in writes
        ],
        "write_latency_s": _latency_summary(write_seconds),
        "search_latency_s": _latency_summary(search_seconds),
        "event_totals": _sum_events(writes),
        "n_queries": len(recalls),
        "recalls": [asdict(r) for r in recalls],
        "hit_at_1": _hit_at_k(recalls, 1),
        "hit_at_3": _hit_at_k(recalls, 3),
        "hit_at_5": _hit_at_k(recalls, 5),
        "mrr": _mrr(recalls),
        "stale_hit_queries": [r.query_id for r in recalls if r.stale_hit],
        "stored_memory_count": len(stored),
        "stored_memories": [
            {"poc_fact_id": (m.get("metadata") or {}).get("poc_fact_id", ""), "memory": m.get("memory", "")}
            for m in stored
        ],
        "storage": _pg_collection_stats(cfg, password)
        | {
            "history_db_bytes": history_path.stat().st_size if history_path.exists() else 0,
            "history_db_path": str(history_path),
        },
    }
    return result_payload


def _pg_collection_stats(cfg: PoCConfig, password: str) -> dict[str, object]:
    """pgvector 侧存储占用：表行数 + 总关系体积（含 TOAST/索引）。失败不推翻写入/检索结论。"""
    import psycopg2
    from psycopg2 import sql

    try:
        conn = psycopg2.connect(
            host=cfg.pg_host, port=cfg.pg_port, dbname=cfg.pg_db, user=cfg.pg_user, password=password
        )
    except Exception as exc:  # noqa: BLE001 — 统计失败仅记录
        return {"pg_error": str(exc)}
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT count(*) FROM {tbl}").format(tbl=sql.Identifier(cfg.collection)))
            rows = cur.fetchone()[0]
            cur.execute(
                "SELECT pg_total_relation_size(c.oid) FROM pg_class c WHERE c.relname = %s", (cfg.collection,)
            )
            total_bytes = cur.fetchone()[0]
            return {"pg_rows": rows, "pg_total_bytes": int(total_bytes), "pg_total_pretty": f"{total_bytes / MB:.2f}MB"}
    except Exception as exc:  # noqa: BLE001 — 统计失败仅记录
        return {"pg_error": str(exc)}
    finally:
        conn.close()


def _sum_events(writes: list[WriteRecord]) -> dict[str, int]:
    """事件计数汇总（ADD/UPDATE/DELETE/NONE 全口径）。"""
    totals: dict[str, int] = {}
    for w in writes:
        for event, count in w.events.items():
            totals[event] = totals.get(event, 0) + count
    return totals


def _hit_at_k(recalls: list[RecallRecord], k: int) -> float:
    """hit@k：命中且排名 ≤ k 的 query 占比。"""
    if not recalls:
        return 0.0
    return round(sum(1 for r in recalls if r.hit and 0 < r.hit_rank <= k) / len(recalls), 4)


def _mrr(recalls: list[RecallRecord]) -> float:
    """MRR：命中排名倒数均值（未命中记 0）。"""
    if not recalls:
        return 0.0
    return round(sum(1 / r.hit_rank for r in recalls if r.hit) / len(recalls), 4)


def _latency_summary(seconds: list[float]) -> dict[str, float]:
    """延迟摘要：mean/p50/p95/max（小样例口径，p95=排序取 95 分位最近值）。"""
    if not seconds:
        return {}
    ordered = sorted(seconds)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {
        "mean": round(statistics.fmean(ordered), 3),
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[idx], 3),
        "max": round(ordered[-1], 3),
    }


# ---------------------------------------------------------------------------
# Graphiti 结构化评估路（pip 层实测；运行时未实测）
# ---------------------------------------------------------------------------

# 文档级结构化结论（依据：graphiti-core wheel METADATA + Neo4j 官方版次能力矩阵 + 架构锚点 §3.2。
# 均为「未实测运行时」口径，报告与 JSON 中显式标注 measured=false。）
GRAPHITI_ASSESSMENT: Final[dict[str, object]] = {
    "neo4j_ce_compatibility": {
        "driver_requirement": "neo4j>=5.26.0（wheel METADATA）；服务端 CE 5.26+/2025.x 可承载",
        "conclusion": "社区版兼容：可运行；但 CE 无多 database（单库+system）、无细粒度 RBAC、无热备——"
        "多租户只能靠 graphiti group_id 属性级隔离（设计 §1 裁决框预警成立）",
        "measured": False,
    },
    "resource_profile": {
        "server": "Neo4j = JVM 常驻：CE 最小可行画像 heap ~512MB + page cache ~512MB，加开销实际 ~1.5-2GB RAM",
        "conflict_with_lite": "lite 档五存储=PG+Redis+MinIO（无 Neo4j）；16G 档主机再驻一个 JVM 与 vLLM/嵌入争内存，"
        "且 PoC④ 当日实测主栈 postgres:16-alpine 连 vector 扩展都未带——lite 档存储面与 Graphiti 假设差距显著",
        "measured": False,
    },
    "alternative_backends": {
        "extras": "falkordb / falkordblite（Redis 协议）/ kuzu（嵌入式零服务）extra 可选——kuzu 提供免服务图存储，"
        "是 lite 档规避 Neo4j 的候选路径，但偏离 Graphiti 主推后端，成熟度未实测",
        "measured": False,
    },
    "full_tier_fit": "full 档 M5 起本就规划 Neo4j（L4 属性图，锚点 §3.2）；Graphiti 届时可与其共库部署，"
    "CE 单库约束下两域同图、以 label/group_id 划界——L3 决策可推迟到 M5 前再裁",
    "measured": False,
}


def eval_graphiti() -> dict[str, object]:
    """Graphiti 依赖树事实（pip 层实测）+ 文档级评估（运行时未实测，assessment 内逐项标注）。"""
    facts: dict[str, object] = {"engine": "graphiti", "runtime_measured": False}
    with tempfile.TemporaryDirectory(prefix="poc4_graphiti_") as tmp:
        download = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pip", "download", GRAPHITI_PKG, "--no-deps", "-d", tmp, "-q"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if download.returncode != 0:
            facts["wheel_error"] = download.stderr.strip()[-500:]
            return facts
        wheels = list(Path(tmp).glob("*.whl"))
        if not wheels:
            facts["wheel_error"] = "no wheel downloaded"
            return facts
        with zipfile.ZipFile(wheels[0]) as zf:
            meta_entry = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
            metadata_text = zf.read(meta_entry).decode("utf-8", errors="replace")
        requires_dist = sorted(line for line in metadata_text.splitlines() if line.startswith("Requires-Dist"))
        requires_python = next(
            (
                line.split(":", 1)[1].strip()
                for line in metadata_text.splitlines()
                if line.startswith("Requires-Python")
            ),
            "",
        )
        facts["wheel"] = wheels[0].name
        facts["requires_python"] = requires_python
        facts["core_requires"] = [r for r in requires_dist if "extra ==" not in r]
        facts["requires_dist_with_extras"] = requires_dist
    dry = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pip", "install", "--dry-run", GRAPHITI_PKG],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    would = [line for line in dry.stdout.splitlines() if "Would install" in line]
    conflicts = [
        line
        for line in (dry.stdout + dry.stderr).splitlines()
        if "conflict" in line.lower() or "incompatible" in line.lower()
    ]
    facts["pip_dry_run"] = {
        "returncode": dry.returncode,
        "would_install": would,
        "conflict_lines": conflicts,
        "conclusion": (
            "本机 Python 3.13 现场解析无冲突（would install 仅自身=依赖全部已满足）"
            if dry.returncode == 0 and not conflicts
            else "存在解析输出，见 would_install/conflict_lines"
        ),
    }
    facts["assessment"] = GRAPHITI_ASSESSMENT
    return facts


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """CLI 参数（LLM/嵌入/pgvector 连接 + 范围控制）。"""
    parser = argparse.ArgumentParser(description="PoC4 L2 记忆引擎选型 harness（mem0 live + Graphiti 评估）")
    parser.add_argument("--llm-base-url", default=None, help="LLM OpenAI 兼容端点（缺省 .env OA_LLM_BASE_URL）")
    parser.add_argument("--llm-model", default=None, help="LLM 模型名（缺省 .env OA_LLM_MODEL）")
    parser.add_argument("--embed-base-url", default="http://127.0.0.1:18002/v1", help="TEI OpenAI 兼容嵌入端点")
    parser.add_argument("--embed-model", default="bge-m3", help="嵌入模型名")
    parser.add_argument("--embed-dims", type=int, default=1024, help="嵌入维度（bge-m3=1024）")
    parser.add_argument("--pg-host", default="127.0.0.1", help="pgvector 主机（PoC 专用容器）")
    parser.add_argument("--pg-port", type=int, default=5433, help="pgvector 端口")
    parser.add_argument("--pg-db", default="onto", help="pgvector 数据库")
    parser.add_argument("--pg-user", default="onto", help="pgvector 用户")
    parser.add_argument("--pg-password", default="onto_dev", help="pgvector 密码（不落结果 JSON）")
    parser.add_argument("--collection", default="poc4_mem0", help="mem0 collection 表名")
    parser.add_argument("--no-reset", action="store_true", help="不清空 collection（默认清空=可复现基线）")
    parser.add_argument("--skip-mem0", action="store_true", help="跳过 mem0 实测")
    parser.add_argument("--skip-graphiti", action="store_true", help="跳过 Graphiti 评估")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条写入（自检用，检索仍全跑）")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="JSON 结果输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口：跑 mem0 live（可跳）+ Graphiti 评估（可跳），落 JSON 并打印摘要。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    args = build_parser().parse_args(argv)
    cfg = build_config(args)

    result: dict[str, object] = {
        "meta": {
            "generated_at": datetime.now(UTC).isoformat(),
            "poc": "PoC④ L2 记忆引擎选型（mem0 vs Graphiti）",
            "python_version": sys.version.split()[0],
            "platform_stack": (
                "lite 档（PG+Redis+MinIO，无 Neo4j/Milvus）；"
                "pgvector=PoC 专用临时容器 oa-poc-pgvector:5433（主栈镜像无 vector 扩展）"
            ),
            "hit_rule": (
                "主=payload.poc_fact_id∈gold；备=归一化文本包含某 gold 关键词组全部成员；"
                "stale=陈旧关键词命中（更新未收敛信号）"
            ),
            "graphiti_runtime_measured": False,
        },
    }

    if not args.skip_mem0:
        print(
            f"[mem0] llm={cfg.llm_base_url} model={cfg.llm_model} embed={cfg.embed_base_url} "
            f"pgvector={cfg.pg_host}:{cfg.pg_port}/{cfg.pg_db}",
            flush=True,
        )
        result["mem0"] = run_mem0(cfg, args.pg_password, reset=not args.no_reset, limit=args.limit)
    if not args.skip_graphiti:
        print("[graphiti] 依赖树评估（pip 层实测；运行时未实测）", flush=True)
        result["graphiti"] = eval_graphiti()

    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[落盘] {args.output}（{args.output.stat().st_size / MB:.2f}MB）", flush=True)

    mem0_block = result.get("mem0")
    if isinstance(mem0_block, dict) and mem0_block.get("measured"):
        print(f"mem0 写入延迟(s)：{mem0_block.get('write_latency_s')}", flush=True)
        print(f"mem0 检索延迟(s)：{mem0_block.get('search_latency_s')}", flush=True)
        print(
            "mem0 命中：hit@1={h1} hit@3={h3} hit@5={h5} mrr={mrr} stale={stale}".format(
                h1=mem0_block.get("hit_at_1"),
                h3=mem0_block.get("hit_at_3"),
                h5=mem0_block.get("hit_at_5"),
                mrr=mem0_block.get("mrr"),
                stale=mem0_block.get("stale_hit_queries"),
            ),
            flush=True,
        )
        print(f"mem0 事件总计：{mem0_block.get('event_totals')}", flush=True)
        print(f"mem0 存储：{mem0_block.get('storage')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
