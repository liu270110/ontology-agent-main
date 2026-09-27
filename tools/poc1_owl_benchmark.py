#!/usr/bin/env python3
"""PoC1 OWL 推理基准（实施计划 13 篇 2.4；SLA 依据 docs/architecture/05 §3）。

合成确定性标本图（TBox：subClassOf 链 + domain/range + disjointWith + inverseOf；
ABox：实例 + feeds 关系 + hasStatus/hasCapacity 数据属性），测量三项：
  1. rdflib Turtle 解析基线（内存字节流，扣磁盘 IO）；
  2. owlrl OWL 2 RL 物化（wall time + tracemalloc/RSS 峰值内存）；
  3. pySHACL 校验（1 万档 20 次取 P50/P99；10 万档 3 次）。

用法：
  python tools/poc1_owl_benchmark.py             # 1 万 + 10 万全量（每档 3 轮）
  python tools/poc1_owl_benchmark.py --quick     # 仅 1 万档
  python tools/poc1_owl_benchmark.py --smoke     # 管线自检：2000 三元组 x 1 轮（不入报告）

结果写 stdout（markdown 矩阵）与 tools/poc1_results.json（每档完成即增量落盘）。
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import platform
import random
import statistics
import sys
import threading
import time
import tracemalloc
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import ClassVar, Final

import owlrl
import pyshacl
import rdflib
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SH, XSD

try:  # psutil 可缺省：缺则仅报 tracemalloc 口径，RSS 列记 "-"（报告注明口径差异）
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

EX: Final = Namespace("https://example.org/poc1/")
FEEDS: Final[URIRef] = EX.op0  # 与 EX.op1 互为 inverseOf（见 build_tbox）

CHAINS: Final = 6  # 领域链数
CHAIN_DEPTH: Final = 8  # 每链 subClassOf 深度（Class{c}_1..Class{c}_8）
ALT_LEVELS: Final = (2, 4, 6, 8)  # 在这些层级插入 disjointWith 兄弟类
STATUS_CYCLE: Final = ("NORMAL", "NORMAL", "NORMAL", "DEGRADED", "FAULTED")
STATUS_DEFECT: Final = "UNKNOWN_STATUS"  # 每 100 个实例 1 个，触发 sh:in 违规
CAPACITY_DEFECT: Final = 187  # 每 97 个实例 1 个，触发 sh:maxInclusive 违规
ABOX_TRIPLES_PER_INSTANCE: Final = 5  # type(1) + feeds(2) + hasStatus(1) + hasCapacity(1)

SCALE_TARGETS: Final[dict[str, int]] = {"10k": 10_000, "100k": 100_000}
SMOKE_TARGET: Final = 2_000
# tracemalloc 开销实测显著（1 万档第 1 轮 152s vs 热身轮 23s，约 6.6x）：
# 仅对不超过该目标量的规模档开启，更大规模只采 RSS 口径（报告注明）
TRACEMALLOC_MAX_TARGET: Final = 25_000
DEFAULT_SEED: Final = 42
MB: Final = 1024 * 1024
WATCHDOG_INTERVAL_S: Final = 30.0
WATCHDOG_WARN_S: Final = 600.0  # 单阶段超 10 分钟显式告警（任务书要求）

# SLA 目标（docs/architecture/05 §3）：仅供 JSON 记录对照，脚本不做判定
SLA_TARGETS: Final[dict[str, object]] = {
    "shacl_10k_p99_ms": 100,
    "owl_reasoning_async_p95_s": 5,
}


# ---------------------------------------------------------------------------
# 合成数据生成器（确定性：index 驱动 + 固定种子 RNG）
# ---------------------------------------------------------------------------


def _leaf_class(index: int) -> URIRef:
    """实例 i 挂到第 i%CHAINS 条链的叶子类上。"""
    return EX[f"Class{index % CHAINS}_{CHAIN_DEPTH}"]


def build_tbox() -> Graph:
    """标本 TBox：6 条 8 级 subClassOf 链 + disjointWith 兄弟 + 18 对象属性（8 对 inverseOf）。"""
    g = Graph()
    g.bind("poc", EX)
    g.bind("owl", OWL)
    root: URIRef = EX.DomainRoot
    g.add((root, RDF.type, OWL.Class))
    for c in range(CHAINS):
        droot = EX[f"Domain{c}"]
        g.add((droot, RDF.type, OWL.Class))
        g.add((droot, RDFS.subClassOf, root))
        prev: URIRef = droot
        for level in range(1, CHAIN_DEPTH + 1):
            cur = EX[f"Class{c}_{level}"]
            g.add((cur, RDF.type, OWL.Class))
            g.add((cur, RDFS.subClassOf, prev))
            if level in ALT_LEVELS:
                alt = EX[f"Alt{c}_{level}"]
                g.add((alt, RDF.type, OWL.Class))
                g.add((alt, RDFS.subClassOf, prev))
                g.add((cur, OWL.disjointWith, alt))
                g.add((alt, OWL.disjointWith, cur))
            prev = cur
    props: list[URIRef] = []
    for p in range(18):
        prop = EX[f"op{p}"]
        props.append(prop)
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.domain, root))
        g.add((prop, RDFS.range, root))
    for k in range(8):  # 8 对互逆属性（双向声明，owlrl 双向推断）
        g.add((props[2 * k], OWL.inverseOf, props[2 * k + 1]))
        g.add((props[2 * k + 1], OWL.inverseOf, props[2 * k]))
    for name, rng_val in (("hasStatus", XSD.string), ("hasCapacity", XSD.integer)):
        prop = EX[name]
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, RDFS.domain, root))
        g.add((prop, RDFS.range, rng_val))
    g.add((EX.note, RDF.type, OWL.AnnotationProperty))
    return g


def _enum_list(g: Graph, chain: int, values: list[str]) -> URIRef:
    """构造 sh:in 用的 RDF list（URI 节点，保证生成确定性）。"""
    nodes = [EX[f"Enum{chain}_{k}"] for k in range(len(values))]
    for k, (node, value) in enumerate(zip(nodes, values, strict=True)):
        g.add((node, RDF.first, Literal(value, datatype=XSD.string)))
        g.add((node, RDF.rest, nodes[k + 1] if k + 1 < len(nodes) else RDF.nil))
    return nodes[0]


def build_shapes() -> Graph:
    """SHACL shapes：每链一个 NodeShape（targetClass 中层类，靠 subClassOf* 覆盖全部后代实例）。"""
    g = Graph()
    g.bind("sh", SH)
    g.bind("poc", EX)
    for c in range(CHAINS):
        shape = EX[f"Shape{c}"]
        g.add((shape, RDF.type, SH.NodeShape))
        g.add((shape, SH.targetClass, EX[f"Class{c}_3"]))
        ps_status = EX[f"Shape{c}_status"]
        g.add((shape, SH.property, ps_status))
        g.add((ps_status, RDF.type, SH.PropertyShape))
        g.add((ps_status, SH.path, EX.hasStatus))
        g.add((ps_status, SH.datatype, XSD.string))
        g.add((ps_status, SH.minCount, Literal(1)))
        g.add((ps_status, SH.maxCount, Literal(1)))
        g.add((ps_status, SH["in"], _enum_list(g, c, ["NORMAL", "DEGRADED", "FAULTED"])))
        ps_cap = EX[f"Shape{c}_capacity"]
        g.add((shape, SH.property, ps_cap))
        g.add((ps_cap, RDF.type, SH.PropertyShape))
        g.add((ps_cap, SH.path, EX.hasCapacity))
        g.add((ps_cap, SH.datatype, XSD.integer))
        g.add((ps_cap, SH.minInclusive, Literal(0)))
        g.add((ps_cap, SH.maxInclusive, Literal(100)))
        g.add((ps_cap, SH.maxCount, Literal(1)))
        ps_feeds = EX[f"Shape{c}_feeds"]
        g.add((shape, SH.property, ps_feeds))
        g.add((ps_feeds, RDF.type, SH.PropertyShape))
        g.add((ps_feeds, SH.path, FEEDS))
        g.add((ps_feeds, SH["class"], EX.DomainRoot))
        g.add((ps_feeds, SH.maxCount, Literal(3)))
    return g


def build_graph(target: int, seed: int) -> tuple[Graph, int]:
    """构建 TBox+ABox 合并图，按三元组总数精确校准到 target（不足处用 note 填充）。"""
    g = build_tbox()
    tbox_n = len(g)
    rng = random.Random(seed)
    i = 0
    while len(g) + ABOX_TRIPLES_PER_INSTANCE <= target:
        inst = EX[f"Inst{i}"]
        g.add((inst, RDF.type, _leaf_class(i)))
        if i > 0:
            g.add((inst, FEEDS, EX[f"Inst{i - 1}"]))
            other = i // 2
            if other != i - 1:
                g.add((inst, FEEDS, EX[f"Inst{other}"]))
        status = STATUS_DEFECT if i % 100 == 7 else STATUS_CYCLE[i % len(STATUS_CYCLE)]
        g.add((inst, EX.hasStatus, Literal(status, datatype=XSD.string)))
        cap = CAPACITY_DEFECT if i % 97 == 13 else rng.randrange(0, 101)
        g.add((inst, EX.hasCapacity, Literal(cap, datatype=XSD.integer)))
        i += 1
    j = 0
    while len(g) < target:
        g.add((EX[f"Inst{max(i - 1, 0)}"], EX.note, Literal(f"note-{j}", datatype=XSD.string)))
        j += 1
    return g, tbox_n


# ---------------------------------------------------------------------------
# 测量基建：看门狗 / RSS 采样 / 计时统计
# ---------------------------------------------------------------------------


class PhaseWatchdog:
    """后台线程每 30s 打印当前阶段进度；单阶段超 10 分钟显式告警（不中断）。"""

    INTERVAL_S: Final = WATCHDOG_INTERVAL_S
    WARN_S: Final = WATCHDOG_WARN_S
    _lock: ClassVar[threading.Lock] = threading.Lock()
    _current: ClassVar[tuple[str, float] | None] = None
    _stop: ClassVar[threading.Event] = threading.Event()

    @classmethod
    def start_global(cls) -> None:
        threading.Thread(target=cls._loop, name="poc1-watchdog", daemon=True).start()

    @classmethod
    def stop_global(cls) -> None:
        cls._stop.set()

    @classmethod
    def _loop(cls) -> None:
        while not cls._stop.wait(cls.INTERVAL_S):
            with cls._lock:
                current = cls._current
            if current is None:
                continue
            name, started = current
            elapsed = time.monotonic() - started
            mark = "，已超过 10 分钟" if elapsed >= cls.WARN_S else ""
            print(f"[进度] {name} 已运行 {elapsed:.0f}s{mark}", flush=True)

    def __init__(self, name: str) -> None:
        self._name = name

    def __enter__(self) -> PhaseWatchdog:
        with PhaseWatchdog._lock:
            PhaseWatchdog._current = (self._name, time.monotonic())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        with PhaseWatchdog._lock:
            PhaseWatchdog._current = None


class RssPeakSampler:
    """阶段内以固定间隔采样进程 RSS，报峰值（psutil 缺失时 peak 恒为 0，由调用方跳过）。"""

    def __init__(self, interval_s: float = 0.1) -> None:
        self._interval_s = interval_s
        self._peak = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> RssPeakSampler:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        if psutil is None:
            return
        proc = psutil.Process()
        while not self._stop.wait(self._interval_s):
            self._peak = max(self._peak, proc.memory_info().rss)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if psutil is not None:
            self._peak = max(self._peak, psutil.Process().memory_info().rss)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    @property
    def peak_rss_bytes(self) -> int:
        return self._peak


def percentile(values: list[float], pct: float) -> float:
    """最近秩法百分位。"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = math.ceil(pct / 100.0 * len(ordered))
    return ordered[min(len(ordered), max(1, rank)) - 1]


def fmt_mb(n_bytes: int | None) -> str:
    return "-" if n_bytes is None else f"{n_bytes / MB:.0f}MB"


# ---------------------------------------------------------------------------
# 被测操作
# ---------------------------------------------------------------------------


def parse_graph(data: str) -> Graph:
    """纯 rdflib Turtle 解析（内存字节流，无磁盘 IO）。"""
    g = Graph()
    g.parse(data=data, format="turtle")
    return g


def materialize(g: Graph) -> None:
    """owlrl OWL 2 RL 物化（原地展开至不动点）。"""
    closer = owlrl.DeductiveClosure(owlrl.OWLRL_Semantics)
    closer.expand(g)


def shacl_validate(g: Graph, shapes: Graph) -> tuple[bool, int]:
    """pySHACL 校验（显式 shacl_graph 关键字，规避 0.40 双关键字静默坑；返回 conforms 与违规数）。"""
    conforms, results, _text = pyshacl.validate(
        data_graph=g,
        shacl_graph=shapes,
        inference="none",
        advanced=False,
    )
    violations = sum(1 for _ in results.subjects(RDF.type, SH.ValidationResult))
    return bool(conforms), violations


# ---------------------------------------------------------------------------
# 结果模型与输出
# ---------------------------------------------------------------------------


@dataclass
class ScaleResult:
    """单规模档测量结果。"""

    target_triples: int
    actual_triples: int
    tbox_triples: int
    parse_seconds: list[float] = field(default_factory=list)
    materialize_seconds: list[float] = field(default_factory=list)
    materialize_tracemalloc_peak_bytes: list[int] = field(default_factory=list)
    materialize_rss_peak_bytes: list[int] = field(default_factory=list)
    materialized_triples: int = 0
    shacl_seconds: list[float] = field(default_factory=list)
    shacl_tracemalloc_peak_bytes: int | None = None
    shacl_rss_peak_bytes: int | None = None
    shacl_violations: int = 0
    shacl_conforms: bool = False
    rounds_completed: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "target_triples": self.target_triples,
            "actual_triples": self.actual_triples,
            "tbox_triples": self.tbox_triples,
            "rounds_completed": self.rounds_completed,
            "parse_seconds": self.parse_seconds,
            "parse_seconds_median": statistics.median(self.parse_seconds) if self.parse_seconds else None,
            "materialize_seconds": self.materialize_seconds,
            "materialize_seconds_median": (
                statistics.median(self.materialize_seconds) if self.materialize_seconds else None
            ),
            "materialize_tracemalloc_peak_bytes": self.materialize_tracemalloc_peak_bytes,
            "materialize_rss_peak_bytes": self.materialize_rss_peak_bytes,
            "materialized_triples": self.materialized_triples,
            "shacl_seconds": self.shacl_seconds,
            "shacl_seconds_p50": percentile(self.shacl_seconds, 50) if self.shacl_seconds else None,
            "shacl_seconds_p99": percentile(self.shacl_seconds, 99) if self.shacl_seconds else None,
            "shacl_tracemalloc_peak_bytes": self.shacl_tracemalloc_peak_bytes,
            "shacl_rss_peak_bytes": self.shacl_rss_peak_bytes,
            "shacl_violations": self.shacl_violations,
            "shacl_conforms": self.shacl_conforms,
        }


def collect_meta(seed: int, smoke: bool) -> dict[str, object]:
    """环境与口径元数据。"""
    versions: dict[str, str] = {
        "python": platform.python_version(),
        "rdflib": rdflib.__version__,
        "owlrl": owlrl.__version__,
        "pyshacl": pyshacl.__version__,
    }
    if psutil is not None:
        versions["psutil"] = psutil.__version__
    env_line = " ".join(f"{k}={v}" for k, v in versions.items())
    meta: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "smoke_mode": smoke,
        "seed": seed,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_physical": psutil.cpu_count(logical=False) if psutil is not None else None,
        "cpu_logical": psutil.cpu_count() if psutil is not None else None,
        "ram_total_gb": round(psutil.virtual_memory().total / (1024**3), 1) if psutil is not None else None,
        "versions": versions,
        "env_line": env_line,
        "measurement_notes": [
            "解析基线=内存字节流 Turtle 解析（无磁盘 IO）；每轮新解析新图",
            "owlrl=DeductiveClosure(OWLRL_Semantics) 原地物化；tracemalloc 仅第 1 轮且仅"
            f" <= {TRACEMALLOC_MAX_TARGET:,} 三元组档开启（10k 实测追踪开销约 6.6x：152s vs 热身 23s）",
            "RSS 峰值=阶段内 0.1s 间隔采样进程 RSS 的最大值（含 Python 堆外与解释器基线）",
            "pySHACL 对未物化原始图校验（与推理同图同口径）；inference=none",
            "数据形态：TBox 282 三元组固定（6 链 x 8 级 + disjointWith + 8 对 inverseOf）；ABox 按目标总量精确填充",
            f"SLA 对照目标：{SLA_TARGETS}",
        ],
    }
    return meta


def write_json(path: Path, meta: dict[str, object], results: list[ScaleResult]) -> None:
    payload = {"meta": meta, "scales": [r.to_dict() for r in results]}
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"[落盘] {path}", flush=True)


def print_markdown(results: list[ScaleResult]) -> None:
    print("\n## 结果矩阵（markdown）\n", flush=True)
    print(
        "| 规模档(三元组) | 阶段 | 轮次/次数 | 中位耗时 | P50 | P99 | tracemalloc峰值 | RSS峰值 |",
        flush=True,
    )
    print("|---|---|---|---|---|---|---|---|", flush=True)
    for r in results:
        scale_label = f"{r.actual_triples:,}"
        if r.parse_seconds:
            print(
                f"| {scale_label} | rdflib 解析(基线) | {len(r.parse_seconds)} 轮 "
                f"| {statistics.median(r.parse_seconds):.2f}s | - | - | - | - |",
                flush=True,
            )
        if r.materialize_seconds:
            rss = max(r.materialize_rss_peak_bytes) if r.materialize_rss_peak_bytes else None
            tm = r.materialize_tracemalloc_peak_bytes[0] if r.materialize_tracemalloc_peak_bytes else None
            print(
                f"| {scale_label} | owlrl 物化 | {len(r.materialize_seconds)} 轮 "
                f"| {statistics.median(r.materialize_seconds):.2f}s | - | - | {fmt_mb(tm)} | {fmt_mb(rss)} |",
                flush=True,
            )
        if r.shacl_seconds:
            rss = r.shacl_rss_peak_bytes
            p99 = percentile(r.shacl_seconds, 99)
            print(
                f"| {scale_label} | pySHACL 校验 | {len(r.shacl_seconds)} 次 "
                f"| {statistics.median(r.shacl_seconds):.3f}s | {percentile(r.shacl_seconds, 50):.3f}s "
                f"| {p99:.3f}s | {fmt_mb(r.shacl_tracemalloc_peak_bytes)} | {fmt_mb(rss)} |",
                flush=True,
            )
        if r.materialized_triples:
            ratio = r.materialized_triples / r.actual_triples if r.actual_triples else 0.0
            print(
                f"\n档位 {scale_label} 备注：物化后 {r.materialized_triples:,} 三元组"
                f"（膨胀 {ratio:.2f}x）；SHACL conforms={r.shacl_conforms}，违规数={r.shacl_violations}",
                flush=True,
            )


# ---------------------------------------------------------------------------
# 基准执行
# ---------------------------------------------------------------------------


def run_scale(target: int, *, rounds: int, shacl_runs: int, seed: int, shapes: Graph) -> ScaleResult:
    print(f"\n=== 规模档：目标 {target:,} 三元组 ===", flush=True)
    t_start = time.perf_counter()
    g, tbox_n = build_graph(target, seed)
    result = ScaleResult(target_triples=target, actual_triples=len(g), tbox_triples=tbox_n)
    print(
        f"[生成] 实际 {result.actual_triples:,} 三元组（TBox {tbox_n}，生成 {time.perf_counter() - t_start:.1f}s）",
        flush=True,
    )

    with PhaseWatchdog("Turtle 序列化（准备解析基线输入）"):
        data = g.serialize(format="turtle").encode("utf-8")
    del g
    gc.collect()

    trace_malloc_on = target <= TRACEMALLOC_MAX_TARGET
    for rnd in range(rounds):
        with PhaseWatchdog(f"第 {rnd + 1}/{rounds} 轮 rdflib 解析"):
            t0 = time.perf_counter()
            g2 = parse_graph(data.decode("utf-8"))
            result.parse_seconds.append(time.perf_counter() - t0)

        if rnd == 0 and trace_malloc_on:
            tracemalloc.start()
        with RssPeakSampler() as sampler, PhaseWatchdog(f"第 {rnd + 1}/{rounds} 轮 owlrl 物化"):
            t0 = time.perf_counter()
            materialize(g2)
            result.materialize_seconds.append(time.perf_counter() - t0)
        if rnd == 0 and trace_malloc_on:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            result.materialize_tracemalloc_peak_bytes.append(peak)
        result.materialize_rss_peak_bytes.append(sampler.peak_rss_bytes)
        result.materialized_triples = len(g2)
        result.rounds_completed = rnd + 1
        print(
            f"  轮 {rnd + 1}: 解析 {result.parse_seconds[-1]:.2f}s | "
            f"物化 {result.materialize_seconds[-1]:.2f}s | 物化后 {len(g2):,} 三元组 | "
            f"RSS峰值 {fmt_mb(sampler.peak_rss_bytes)}",
            flush=True,
        )
        del g2
        gc.collect()

    with PhaseWatchdog("解析 SHACL 待校验图"):
        g3 = parse_graph(data.decode("utf-8"))
    for run in range(shacl_runs):
        if run == 0 and trace_malloc_on:
            tracemalloc.start()
        with RssPeakSampler() as sampler, PhaseWatchdog(f"pySHACL 校验 第 {run + 1}/{shacl_runs} 次"):
            t0 = time.perf_counter()
            result.shacl_conforms, result.shacl_violations = shacl_validate(g3, shapes)
            result.shacl_seconds.append(time.perf_counter() - t0)
        if run == 0 and trace_malloc_on:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            result.shacl_tracemalloc_peak_bytes = peak
            result.shacl_rss_peak_bytes = sampler.peak_rss_bytes
        elif run == 0:
            result.shacl_rss_peak_bytes = sampler.peak_rss_bytes
        print(
            f"  SHACL 第 {run + 1} 次: {result.shacl_seconds[-1]:.3f}s "
            f"(conforms={result.shacl_conforms}, violations={result.shacl_violations})",
            flush=True,
        )
    del g3
    gc.collect()
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PoC1 OWL 推理与 SHACL 门禁基准")
    parser.add_argument("--scale", choices=sorted(SCALE_TARGETS), action="append", help="规模档，可重复；缺省=两档全跑")
    parser.add_argument("--quick", action="store_true", help="仅跑 1 万档")
    parser.add_argument("--smoke", action="store_true", help="管线自检：2000 三元组 x1 轮（不入报告）")
    parser.add_argument("--rounds", type=int, default=3, help="每档重复轮数（默认 3）")
    parser.add_argument("--shacl-runs-10k", type=int, default=20, help="1 万档 SHACL 次数（默认 20）")
    parser.add_argument("--shacl-runs-100k", type=int, default=3, help="10 万档 SHACL 次数（默认 3）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="确定性随机种子（默认 42）")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "poc1_results.json",
        help="JSON 结果输出路径",
    )
    return parser.parse_args(argv)


def _reconfigure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdout()
    args = _parse_args(argv)
    if args.scale:
        scale_names = list(dict.fromkeys(args.scale))
    elif args.quick or args.smoke:
        scale_names = ["10k"]
    else:
        scale_names = ["10k", "100k"]

    shapes = build_shapes()
    meta = collect_meta(seed=args.seed, smoke=args.smoke)
    print(f"[环境] {meta['env_line']} | {meta['platform']}", flush=True)
    PhaseWatchdog.start_global()
    results: list[ScaleResult] = []
    try:
        for name in scale_names:
            target = SMOKE_TARGET if args.smoke else SCALE_TARGETS[name]
            rounds = 1 if args.smoke else args.rounds
            shacl_runs = 3 if args.smoke else (args.shacl_runs_10k if name == "10k" else args.shacl_runs_100k)
            results.append(
                run_scale(target=target, rounds=rounds, shacl_runs=shacl_runs, seed=args.seed, shapes=shapes)
            )
            write_json(args.output, meta, results)
    finally:
        PhaseWatchdog.stop_global()
    print_markdown(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
