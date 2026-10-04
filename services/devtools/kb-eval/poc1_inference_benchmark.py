#!/usr/bin/env python3
"""PoC1 OWL 推理基准 · 电力模板增强版（锚点 docs/architecture/01 §7 PoC① 出口条件）。

与 services/devtools/poc1_owl_benchmark.py 的关系：本脚本是该初版的**迁移增强版**（初版原样保留，
作为通用标本图基准）。差异四点：
  1. 合成图换成「设备—故障—工单」电力模板（类/属性与 services/seeds/power_seed.ttl 同构：
     馈线/开关/变压器/区段/工单/停电事件 + inSection/servesCustomer/dispatchedTo +
     orderNo/hasStatus 数据属性），使基准负载贴近真实电力本体工作面；
  2. 规模档扩为 1k / 5k / 10k / 100k 四档；
  3. 每档**硬超时保护**（10k ≤ 10 分钟、100k ≤ 30 分钟，超时即终止子进程并标注外推值），
     绝不允许无限等待——owlrl 在大图上超时是预期内的裁决证据，不是故障；
  4. 超时档按幂律拟合（对已测档 log-log 最小二乘）给外推值，报告必须与实测分别标注。

测量三阶段（每阶段在独立子进程中执行，进程级超时可终止，且 tracemalloc 不跨阶段污染）：
  1. rdflib Turtle 解析基线（内存字节流，扣磁盘 IO）；
  2. owlrl OWL 2 RL 物化（wall time + tracemalloc 峰值 ≤10k 档 + 子进程 RSS 峰值）；
  3. pySHACL 校验（未物化原始图，inference=none；含确定性注入的违规样本）。

用法（在仓库根目录执行）：
  python services/devtools/kb-eval/poc1_inference_benchmark.py            # 四档全跑（100k 受 30 分钟硬顶保护）
  python services/devtools/kb-eval/poc1_inference_benchmark.py --quick    # 仅 1k 档（管线自检快速档）
  python services/devtools/kb-eval/poc1_inference_benchmark.py --smoke    # 2k 三元组 x1 轮冒烟，不入报告

结果写 stdout（markdown 矩阵）与 services/devtools/kb-eval/poc1_results.json（每档完成即增量落盘）。
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import multiprocessing
import platform
import statistics
import sys
import time
import tracemalloc
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

try:  # psutil 可缺省：缺则 RSS 列记 "-"（报告注明口径差异）
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

PW: Final = "http://ontology-agent.local/o/t1/power#"
EX: Final = "https://example.org/poc1/"

SPEED_DEFECT_STEP: Final = 100  # 每 100 个工单 1 个 orderNo 违规（触发 sh:pattern）
STATUS_DEFECT_STEP: Final = 97  # 每 97 个工单 1 个状态违规（触发 sh:in）
STATUSES: Final = ("created", "created", "dispatched", "dispatched", "in_progress", "resolved", "closed")

SCALE_TARGETS: Final[dict[str, int]] = {"1k": 1_000, "5k": 5_000, "10k": 10_000, "100k": 100_000}
SMOKE_TARGET: Final = 2_000
# 每档硬超时（秒）：锚点要求 10k ≤ 10min、100k ≤ 30min；小档按比例收紧防小图异常卡死
SCALE_BUDGET_S: Final[dict[str, float]] = {"1k": 120.0, "5k": 300.0, "10k": 600.0, "100k": 1800.0}
DEFAULT_ROUNDS: Final[dict[str, int]] = {"1k": 3, "5k": 3, "10k": 2, "100k": 1}
DEFAULT_SHACL_RUNS: Final[dict[str, int]] = {"1k": 10, "5k": 10, "10k": 3, "100k": 1}
# 阶段超时 = 该档预算的分摊份额（materialize 是大头）；各阶段再受"剩余预算"约束
PHASE_SHARE: Final[dict[str, float]] = {"build": 0.10, "parse": 0.15, "materialize": 0.85, "shacl": 0.30}
# tracemalloc 追踪开销实测显著（初版 10k 档约 6.6x），仅对不超过该规模的档开启
TRACEMALLOC_MAX_TARGET: Final = 10_000
MB: Final = 1024 * 1024
POLL_INTERVAL_S: Final = 0.2

SLA_TARGETS: Final[dict[str, object]] = {  # docs/architecture/05 §3，仅供对照，脚本不判定
    "shacl_10k_p99_ms": 100,
    "owl_reasoning_async_p95_s": 5,
}


# ---------------------------------------------------------------------------
# 电力模板图生成器（设备—故障—工单；确定性：纯 index 驱动，无随机数）
# ---------------------------------------------------------------------------


def build_tbox() -> str:
    """电力 TBox（与 power_seed.ttl 同构的精简标本）：对象层级 + 事件/行为 + 属性公理 + disjointWith。"""
    lines = [
        "@prefix pw: <" + PW + "> .",
        "@prefix ex: <" + EX + "> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "pw:PowerDevice a owl:Class ; rdfs:subClassOf ex:PlatformObject .",
        "pw:Feeder a owl:Class ; rdfs:subClassOf pw:PowerDevice .",
        "pw:Transformer a owl:Class ; rdfs:subClassOf pw:PowerDevice .",
        "pw:Substation a owl:Class ; rdfs:subClassOf pw:PowerDevice .",
        "pw:Switch a owl:Class ; rdfs:subClassOf pw:PowerDevice .",
        "pw:ProtectionDevice a owl:Class ; rdfs:subClassOf pw:PowerDevice .",
        "pw:Meter a owl:Class ; rdfs:subClassOf pw:PowerDevice .",
        "pw:DistributionLine a owl:Class ; rdfs:subClassOf pw:PowerDevice .",
        "pw:Transformer owl:disjointWith pw:Meter .",  # R001 同款互斥公理（owl_axiom 路由负载）
        "pw:LineSection a owl:Class ; rdfs:subClassOf ex:PlatformObject .",
        "pw:Customer a owl:Class ; rdfs:subClassOf ex:PlatformObject .",
        "pw:RepairCrew a owl:Class ; rdfs:subClassOf ex:PlatformObject .",
        "pw:CrewMember a owl:Class ; rdfs:subClassOf ex:PlatformObject .",
        "pw:OutageOrder a owl:Class ; rdfs:subClassOf ex:PlatformObject .",
        "pw:OutageReport a owl:Class ; rdfs:subClassOf ex:PlatformObject .",
        "pw:OutageEvent a owl:Class ; rdfs:subClassOf ex:PlatformObject .",
        "pw:OutageConfirmed a owl:Class ; rdfs:subClassOf pw:OutageEvent .",
        "pw:PowerRestored a owl:Class ; rdfs:subClassOf ex:PlatformObject .",
        "pw:StormAlert a owl:Class ; rdfs:subClassOf ex:PlatformObject .",
        "pw:RepairCompleted a owl:Class ; rdfs:subClassOf ex:PlatformObject .",
        "pw:DispatchRepair a owl:Class ; rdfs:subClassOf ex:PlatformAction .",
        "pw:IsolateFault a owl:Class ; rdfs:subClassOf ex:PlatformAction .",
        "pw:RestorePower a owl:Class ; rdfs:subClassOf ex:PlatformAction .",
        # 对象属性（含一对 inverseOf：owlrl 需双向物化，见初版结论）
        "pw:inSection a owl:ObjectProperty ; rdfs:domain pw:PowerDevice ; rdfs:range pw:LineSection .",
        "pw:hasDevice a owl:ObjectProperty ; rdfs:domain pw:LineSection ; rdfs:range pw:PowerDevice ;"
        " owl:inverseOf pw:inSection .",
        "pw:servesCustomer a owl:ObjectProperty ; rdfs:domain pw:Feeder ; rdfs:range pw:Customer .",
        "pw:dispatchedTo a owl:ObjectProperty ; rdfs:domain pw:OutageOrder ; rdfs:range pw:RepairCrew .",
        "pw:crewLead a owl:ObjectProperty ; rdfs:domain pw:RepairCrew ; rdfs:range pw:CrewMember .",
        "pw:affectsFeeder a owl:ObjectProperty ; rdfs:domain pw:OutageEvent ; rdfs:range pw:Feeder .",
        "pw:restoresOrder a owl:ObjectProperty ; rdfs:domain pw:RepairCompleted ; rdfs:range pw:OutageOrder .",
        # 数据属性
        "pw:orderNo a owl:DatatypeProperty ; rdfs:domain pw:OutageOrder ; rdfs:range xsd:string .",
        "pw:hasStatus a owl:DatatypeProperty ; rdfs:domain pw:OutageOrder ; rdfs:range xsd:string .",
        "pw:confirmedAt a owl:DatatypeProperty ; rdfs:domain pw:OutageConfirmed ; rdfs:range xsd:dateTime .",
        "pw:repairCrewAvailable a owl:DatatypeProperty ; rdfs:domain pw:Feeder ; rdfs:range xsd:boolean .",
        "ex:note a owl:AnnotationProperty .",
    ]
    return "\n".join(lines)


def _count_triples(text: str) -> int:
    """统计 Turtle 文本三元组数：每条语句单行、以 " ." 结尾（@prefix 行不计）。"""
    return sum(
        line.count(" ; ") + 1
        for line in text.splitlines()
        if line.rstrip().endswith(".") and not line.lstrip().startswith("@prefix")
    )


def _unit_lines(i: int) -> list[str]:
    """第 i 个「设备—工单—事件」单元：9 条三元组（含确定性违规注入）。"""
    dev_cls = "pw:Switch" if i % 2 == 0 else "pw:Transformer"
    status = "UNKNOWN_STATUS" if i % STATUS_DEFECT_STEP == 13 else STATUSES[i % len(STATUSES)]
    order_no = f"BAD-{i:06d}" if i % SPEED_DEFECT_STEP == 7 else f"OO-{i % 100:02d}{i // 100 % 100:02d}{i % 100:02d}"
    ts = f"2026-04-{i % 28 + 1:02d}T08:{i % 60:02d}:00"
    return [
        f"ex:dev{i} a {dev_cls} ; rdfs:label \"设备D-{i:06d}\" ; pw:inSection ex:sec{i % 64} .",
        f"ex:order{i} a pw:OutageOrder ; rdfs:label \"工单O-{i:06d}\" ;"
        f" pw:orderNo \"{order_no}\" ; pw:hasStatus \"{status}\" .",
        f"ex:ev{i} a pw:OutageConfirmed ; rdfs:label \"事件E-{i:06d}\" ;"
        f" pw:affectsFeeder ex:feeder{i % 6} ; pw:confirmedAt \"{ts}\"^^xsd:dateTime .",
    ]


def build_graph(target: int) -> tuple[str, int]:
    """合成「设备-故障-工单」图并精确校准到 target 条三元组（不足处用 note 填充）。"""
    tbox = build_tbox()
    # TBox 头部实例：6 馈线 + 3 变电站 + 4 班组 + 12 班组成员 + 16 区段 + 24 客户（固定小集合）
    heads = [
        f"ex:feeder{k} a pw:Feeder ; rdfs:label \"馈线F-{k}\" ; pw:repairCrewAvailable"
        f" {'true' if k % 2 == 0 else 'false'} ; pw:servesCustomer ex:cust{k % 24} ."
        for k in range(6)
    ]
    heads += [f"ex:sub{k} a pw:Substation ; rdfs:label \"变电站S-{k}\" ." for k in range(3)]
    heads += [
        f"ex:crew{k} a pw:RepairCrew ; pw:crewLead ex:member{k} ." for k in range(4)
    ]
    heads += [f"ex:member{k} a pw:CrewMember ; rdfs:label \"人员M-{k}\" ." for k in range(12)]
    heads += [f"ex:sec{k} a pw:LineSection ; rdfs:label \"区段L-{k}\" ." for k in range(64)]
    heads += [f"ex:cust{k} a pw:Customer ; rdfs:label \"客户C-{k}\" ." for k in range(24)]
    body = "\n".join(heads)
    n = _count_triples(tbox + "\n" + body)
    units: list[str] = []
    i = 0
    while True:
        unit = "\n".join(_unit_lines(i))
        unit_n = _count_triples(unit)
        if n + unit_n > target:
            break
        units.append(unit)
        n += unit_n
        i += 1
    pad: list[str] = []
    j = 0
    while n < target:
        pad.append(f"ex:dev{i - 1 if i else 0} ex:note \"note-{j}\" .")
        n += 1
        j += 1
    graph_text = tbox + "\n" + body + "\n" + "\n".join(units) + "\n" + "\n".join(pad)
    return graph_text, n


SHAPLES_TTL: Final[str] = """
@prefix pw: <http://ontology-agent.local/o/t1/power#> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
pw:OrderShape a sh:NodeShape ;
    sh:targetClass pw:OutageOrder ;
    sh:property [ sh:path pw:orderNo ; sh:datatype xsd:string ;
                  sh:pattern "^OO-[0-9]{6}$" ; sh:minCount 1 ; sh:maxCount 1 ] ;
    sh:property [ sh:path pw:hasStatus ; sh:datatype xsd:string ;
                  sh:in ( "created" "dispatched" "in_progress" "resolved" "closed" ) ; sh:minCount 1 ] .
pw:DeviceShape a sh:NodeShape ;
    sh:targetClass pw:Switch ;
    sh:property [ sh:path pw:inSection ; sh:class pw:LineSection ; sh:maxCount 2 ] .
pw:FeederShape a sh:NodeShape ;
    sh:targetClass pw:Feeder ;
    sh:property [ sh:path pw:servesCustomer ; sh:class pw:Customer ; sh:maxCount 3 ] .
"""


# ---------------------------------------------------------------------------
# 子进程阶段执行体（spawn 上下文；父进程可 terminate 实现硬超时）
# ---------------------------------------------------------------------------


def _phase_worker(phase: str, payload: dict, use_tracemalloc: bool, send_conn) -> None:  # noqa: ANN001
    """子进程入口：执行单阶段并把结果 dict 经管道发回；异常归一为 {"error": ...}。"""
    try:
        import owlrl
        import pyshacl
        import rdflib
        from rdflib import RDF
        from rdflib.namespace import SH

        result: dict = {"phase": phase}
        if phase == "build":
            turtle, n_triples = build_graph(payload["target"])
            result["turtle"] = turtle
            result["triples"] = n_triples
            result["tbox_triples"] = _count_triples(build_tbox())
        elif phase == "parse":
            t0 = time.perf_counter()
            g = rdflib.Graph()
            g.parse(data=payload["turtle"], format="turtle")
            result["seconds"] = time.perf_counter() - t0
            result["triples"] = len(g)
        elif phase == "materialize":
            g = rdflib.Graph()
            g.parse(data=payload["turtle"], format="turtle")  # 解析不计入推理耗时
            closer = owlrl.DeductiveClosure(owlrl.OWLRL_Semantics)
            if use_tracemalloc:
                tracemalloc.start()
            t0 = time.perf_counter()
            closer.expand(g)
            result["seconds"] = time.perf_counter() - t0
            if use_tracemalloc:
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                result["tracemalloc_peak_bytes"] = peak
            result["materialized_triples"] = len(g)
        elif phase == "shacl":
            g = rdflib.Graph()
            g.parse(data=payload["turtle"], format="turtle")
            shapes = rdflib.Graph()
            shapes.parse(data=SHAPLES_TTL, format="turtle")
            t0 = time.perf_counter()
            conforms, results_graph, _text = pyshacl.validate(
                data_graph=g, shacl_graph=shapes, inference="none", advanced=False
            )
            result["seconds"] = time.perf_counter() - t0
            result["conforms"] = bool(conforms)
            result["violations"] = sum(1 for _ in results_graph.subjects(RDF.type, SH.ValidationResult))
        else:
            result["error"] = f"未知阶段 {phase}"
        send_conn.send(result)
    except MemoryError:
        send_conn.send({"phase": phase, "error": "memory_error"})
    except Exception as exc:  # 子进程异常必须回传而非静默
        send_conn.send({"phase": phase, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        send_conn.close()


def run_phase_in_child(
    ctx: multiprocessing.context.BaseContext,
    phase: str,
    payload: dict,
    *,
    timeout_s: float,
    use_tracemalloc: bool = False,
    label: str = "",
) -> tuple[dict | None, int]:
    """在独立子进程跑一个阶段，返回 (结果或 None, 子进程 RSS 峰值字节)；超时返回 (None, 峰值)。"""
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_phase_worker, args=(phase, payload, use_tracemalloc, send_conn), daemon=True)
    t0 = time.perf_counter()
    proc.start()
    peak_rss = 0
    deadline = t0 + timeout_s
    child = psutil.Process(proc.pid) if psutil is not None else None
    finished = False
    while time.perf_counter() < deadline:
        if recv_conn.poll(POLL_INTERVAL_S):
            finished = True
            break
        if child is not None:
            try:
                peak_rss = max(peak_rss, child.memory_info().rss)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    if not finished:
        proc.terminate()
        proc.join(timeout=5)
        recv_conn.close()
        print(f"  [硬超时] {label or phase} 超过 {timeout_s:.0f}s，子进程已终止", flush=True)
        return None, peak_rss
    result = recv_conn.recv()
    recv_conn.close()
    proc.join(timeout=10)
    if proc.is_alive():  # 发回结果后仍未退出：强杀防僵尸
        proc.terminate()
        proc.join(timeout=5)
    if "error" in result:  # 子进程内异常（含 MemoryError）：按失败档处理并在控制台留痕
        print(f"  [阶段失败] {label or phase}: {result['error']}", flush=True)
        return None, peak_rss
    return result, peak_rss


# ---------------------------------------------------------------------------
# 外推（幂律拟合）：超时档给外推值，必须与实测分列
# ---------------------------------------------------------------------------


def extrapolate(points: list[tuple[int, float]], target_n: int) -> tuple[float | None, float | None]:
    """对 (规模, 耗时) 序列做 log-log 最小二乘幂律拟合 τ=a·n^b，返回 (外推值, 指数b)。"""
    pts = [(n, t) for n, t in points if n > 0 and t > 0]
    if len(pts) < 2:
        return None, None
    xs = [math.log(n) for n, _ in pts]
    ys = [math.log(t) for _, t in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / denom if denom else 0.0
    a = math.exp(my - b * mx)
    return a * target_n**b, b


# ---------------------------------------------------------------------------
# 结果模型与输出
# ---------------------------------------------------------------------------


@dataclass
class PhaseStats:
    """单阶段多轮统计。"""

    seconds: list[float] = field(default_factory=list)
    tracemalloc_peak_bytes: list[int] = field(default_factory=list)
    rss_peak_bytes: list[int] = field(default_factory=list)
    timeouts: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class ScaleResult:
    """单规模档测量结果。"""

    name: str
    target_triples: int
    actual_triples: int = 0
    build_seconds: float = 0.0
    parse: PhaseStats = field(default_factory=PhaseStats)
    materialize: PhaseStats = field(default_factory=PhaseStats)
    shacl: PhaseStats = field(default_factory=PhaseStats)
    shacl_conforms: bool = False
    shacl_violations: int = 0
    materialized_triples: int = 0
    rounds_completed: int = 0
    timeout_phase: str | None = None  # 首个触发硬超时的阶段（该档即标记为超时档）

    def to_dict(self) -> dict[str, object]:
        def phase_dict(p: PhaseStats) -> dict[str, object]:
            return {
                "rounds": len(p.seconds),
                "seconds_median": statistics.median(p.seconds) if p.seconds else None,
                "seconds_all": p.seconds,
                "tracemalloc_peak_bytes": p.tracemalloc_peak_bytes or None,
                "rss_peak_bytes": max(p.rss_peak_bytes) if p.rss_peak_bytes else None,
                "timeouts": p.timeouts,
                **p.extra,
            }

        return {
            "scale": self.name,
            "target_triples": self.target_triples,
            "actual_triples": self.actual_triples,
            "build_seconds": self.build_seconds,
            "parse": phase_dict(self.parse),
            "materialize": phase_dict(self.materialize),
            "shacl": {**phase_dict(self.shacl), "conforms": self.shacl_conforms, "violations": self.shacl_violations},
            "materialized_triples": self.materialized_triples,
            "rounds_completed": self.rounds_completed,
            "timeout_phase": self.timeout_phase,
        }


def collect_meta() -> dict[str, object]:
    """环境与口径元数据。"""
    versions: dict[str, str] = {
        "python": platform.python_version(),
        "rdflib": __import__("rdflib").__version__,
        "owlrl": __import__("owlrl").__version__,
        "pyshacl": __import__("pyshacl").__version__,
    }
    if psutil is not None:
        versions["psutil"] = psutil.__version__
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_physical": psutil.cpu_count(logical=False) if psutil is not None else None,
        "cpu_logical": psutil.cpu_count() if psutil is not None else None,
        "ram_total_gb": round(psutil.virtual_memory().total / 2**30, 1) if psutil is not None else None,
        "versions": versions,
        "scale_budget_s": SCALE_BUDGET_S,
        "sla_targets": SLA_TARGETS,
        "measurement_notes": [
            "数据形态=电力模板（设备-故障-工单）：TBox 与 services/seeds/power_seed.ttl 同构"
            "（含 Transformer×Meter 互斥、inSection/hasDevice inverseOf 对）；"
            "ABox 单元=设备+工单+停电确认事件各 1 实例共 11 三元组，"
            "每 100 工单 1 个 orderNo 违规、每 97 工单 1 个状态违规（确定性注入）",
            "三阶段均在独立子进程中执行：进程级硬超时可终止；tracemalloc 仅第一轮且仅 ≤10k 档开启"
            "（追踪开销实测约 6.6x，见初版 poc1_owl_benchmark.py）",
            "RSS 峰值=父进程对子进程 0.2s 间隔采样的最大值（含解释器基线）",
            "pySHACL 对未物化原始图校验（与推理分离口径）；inference=none",
            f"SLA 对照目标：{SLA_TARGETS}",
        ],
    }


def write_json(path: Path, meta: dict[str, object], results: list[ScaleResult]) -> None:
    payload = {"meta": meta, "scales": [r.to_dict() for r in results]}
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"[落盘] {path}", flush=True)


def fmt_mb(n_bytes: int | None) -> str:
    return "-" if not n_bytes else f"{n_bytes / MB:.0f}MB"


def percentile(values: list[float], pct: float) -> float:
    """最近秩法百分位。"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = math.ceil(pct / 100.0 * len(ordered))
    return ordered[min(len(ordered), max(1, rank)) - 1]


# ---------------------------------------------------------------------------
# 基准执行
# ---------------------------------------------------------------------------


def run_scale(
    ctx: multiprocessing.context.BaseContext,
    name: str,
    target: int,
    *,
    rounds: int,
    shacl_runs: int,
) -> ScaleResult:
    """跑一个规模档：build → parse×rounds → materialize×rounds → shacl×N，全程硬超时保护。"""
    budget = SCALE_BUDGET_S.get(name, 600.0)  # 每档硬超时（秒）
    print(f"\n=== 规模档 {name}：目标 {target:,} 三元组，硬超时 {budget:.0f}s ===", flush=True)
    result = ScaleResult(name=name, target_triples=target)
    t_budget0 = time.perf_counter()

    def remaining() -> float:
        return max(5.0, budget - (time.perf_counter() - t_budget0))

    # 阶段 0：图构建 + Turtle 序列化（子进程内完成，防大图常驻父进程）
    t0 = time.perf_counter()
    res, _rss = run_phase_in_child(
        ctx,
        "build",
        {"target": target},
        timeout_s=min(PHASE_SHARE["build"] * budget, remaining()),
        label=f"{name}/build",
    )
    result.build_seconds = time.perf_counter() - t0
    if res is None:
        result.timeout_phase = "build"
        return result
    turtle, result.actual_triples = res["turtle"], res["triples"]
    result.parse.extra["tbox_triples"] = res.get("tbox_triples")
    print(f"[生成] 实际 {result.actual_triples:,} 三元组（构建+序列化 {result.build_seconds:.1f}s）", flush=True)

    # 阶段 1/2：解析与 owlrl 物化（逐轮独立子进程）
    trace_on = target <= TRACEMALLOC_MAX_TARGET
    for rnd in range(rounds):
        if result.parse.timeouts or result.materialize.timeouts:
            break
        res, rss = run_phase_in_child(
            ctx, "parse", {"turtle": turtle}, timeout_s=min(PHASE_SHARE["parse"] * budget, remaining()),
            label=f"{name}/parse#{rnd + 1}",
        )
        if res is None:
            result.parse.timeouts += 1
            result.timeout_phase = result.timeout_phase or "parse"
            break
        result.parse.seconds.append(res["seconds"])
        result.parse.rss_peak_bytes.append(rss)

        res, rss = run_phase_in_child(
            ctx, "materialize", {"turtle": turtle}, use_tracemalloc=(rnd == 0 and trace_on),
            timeout_s=min(PHASE_SHARE["materialize"] * budget, remaining()), label=f"{name}/owlrl#{rnd + 1}",
        )
        if res is None:
            result.materialize.timeouts += 1
            result.timeout_phase = result.timeout_phase or "materialize"
            break
        result.materialize.seconds.append(res["seconds"])
        result.materialize.rss_peak_bytes.append(rss)
        if "tracemalloc_peak_bytes" in res:
            result.materialize.tracemalloc_peak_bytes.append(res["tracemalloc_peak_bytes"])
        result.materialized_triples = res["materialized_triples"]
        result.rounds_completed = rnd + 1
        print(
            f"  轮 {rnd + 1}: 解析 {result.parse.seconds[-1]:.2f}s | owlrl 物化 {result.materialize.seconds[-1]:.2f}s"
            f" | 物化后 {result.materialized_triples:,} 三元组 | 子进程RSS峰值 {fmt_mb(rss)}",
            flush=True,
        )

    # 阶段 3：pySHACL（未物化图；仅在未超时且预算尚余时执行）
    if not result.timeout_phase:
        for run in range(shacl_runs):
            res, rss = run_phase_in_child(
                ctx, "shacl", {"turtle": turtle}, use_tracemalloc=(run == 0 and trace_on),
                timeout_s=min(PHASE_SHARE["shacl"] * budget, remaining()), label=f"{name}/shacl#{run + 1}",
            )
            if res is None:
                result.shacl.timeouts += 1
                break
            result.shacl.seconds.append(res["seconds"])
            result.shacl.rss_peak_bytes.append(rss)
            if run == 0:
                result.shacl_conforms = res["conforms"]
                result.shacl_violations = res["violations"]
                if "tracemalloc_peak_bytes" in res:
                    result.shacl.tracemalloc_peak_bytes.append(res["tracemalloc_peak_bytes"])
        if result.shacl.seconds:
            print(
                f"  SHACL: P50 {percentile(result.shacl.seconds, 50):.3f}s / "
                f"P99 {percentile(result.shacl.seconds, 99):.3f}s"
                f"（conforms={result.shacl_conforms}, violations={result.shacl_violations}）",
                flush=True,
            )
    del turtle
    gc.collect()
    return result


def print_markdown(results: list[ScaleResult]) -> None:
    """stdout 打印 markdown 矩阵（报告直接引用）。"""
    print("\n## 结果矩阵（markdown）\n", flush=True)
    print("| 档位(三元组) | 阶段 | 轮次 | 中位耗时 | P99 | tracemalloc峰值 | 子进程RSS峰值 | 超时 |", flush=True)
    print("|---|---|---|---|---|---|---|---|", flush=True)
    for r in results:
        tag = f"（超时档：{r.timeout_phase} 阶段触发硬超时，见外推值）" if r.timeout_phase else ""
        rows: list[str] = []
        if r.parse.seconds:
            rows.append(
                f"| {r.actual_triples:,} | rdflib 解析(基线) | {len(r.parse.seconds)} "
                f"| {statistics.median(r.parse.seconds):.2f}s | - | - | {fmt_mb(max(r.parse.rss_peak_bytes))} | - |"
            )
        if r.materialize.seconds:
            tm = r.materialize.tracemalloc_peak_bytes[0] if r.materialize.tracemalloc_peak_bytes else None
            rows.append(
                f"| {r.actual_triples:,} | owlrl 物化 | {len(r.materialize.seconds)} "
                f"| {statistics.median(r.materialize.seconds):.2f}s | - | {fmt_mb(tm)} "
                f"| {fmt_mb(max(r.materialize.rss_peak_bytes))} | {r.materialize.timeouts} |"
            )
        elif r.materialize.timeouts:
            rows.append(f"| {r.actual_triples:,} | owlrl 物化 | 0 | 超时未测 | - | - | - | {r.materialize.timeouts} |")
        if r.shacl.seconds:
            rows.append(
                f"| {r.actual_triples:,} | pySHACL 校验 | {len(r.shacl.seconds)} "
                f"| {statistics.median(r.shacl.seconds):.3f}s | {percentile(r.shacl.seconds, 99):.3f}s "
                f"| {fmt_mb(r.shacl.tracemalloc_peak_bytes[0] if r.shacl.tracemalloc_peak_bytes else None)} "
                f"| {fmt_mb(max(r.shacl.rss_peak_bytes))} | {r.shacl.timeouts} |"
            )
        print("\n".join(rows), flush=True)
        if r.materialized_triples:
            ratio = r.materialized_triples / r.actual_triples if r.actual_triples else 0
            print(
                f"\n档位 {r.actual_triples:,} 备注：物化后 {r.materialized_triples:,} 三元组（膨胀 {ratio:.2f}x）；"
                f"SHACL conforms={r.shacl_conforms}，违规数={r.shacl_violations}{tag}",
                flush=True,
            )
        if r.timeout_phase:
            print(f"{tag}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PoC1 电力模板推理基准（硬超时 + 超时外推）")
    parser.add_argument("--scale", choices=sorted(SCALE_TARGETS), action="append", help="规模档，可重复；缺省全跑")
    parser.add_argument("--quick", action="store_true", help="仅 1k 档")
    parser.add_argument("--smoke", action="store_true", help="2k 三元组 x1 轮冒烟自检（不入报告）")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="JSON 结果输出路径（默认 services/devtools/kb-eval/poc1_results.json）",
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
        scale_names = ["1k"]
    else:
        scale_names = list(SCALE_TARGETS)

    out_path = args.output or (Path(__file__).resolve().parent / "poc1_results.json")
    meta = collect_meta()
    print(f"[环境] {meta['versions']} | {meta['platform']}", flush=True)
    ctx = multiprocessing.get_context("spawn")
    results: list[ScaleResult] = []
    for name in scale_names:
        target = SMOKE_TARGET if args.smoke else SCALE_TARGETS[name]
        rounds = 1 if args.smoke else DEFAULT_ROUNDS[name]
        shacl_runs = 1 if args.smoke else DEFAULT_SHACL_RUNS[name]
        results.append(run_scale(ctx, name, target, rounds=rounds, shacl_runs=shacl_runs))
        write_json(out_path, meta, results)
    print_markdown(results)

    # 超时档外推（幂律拟合；与实测分列，报告必须分开标注）
    measured_mat = [
        (r.target_triples, statistics.median(r.materialize.seconds))
        for r in results
        if r.materialize.seconds and not r.timeout_phase
    ]
    timed_out = [r for r in results if r.materialize.timeouts and not r.materialize.seconds]
    if timed_out and len(measured_mat) >= 2:
        for r in timed_out:
            est, b = extrapolate(measured_mat, r.target_triples)
            if est is not None:
                print(
                    f"\n[外推] {r.name} owlrl 物化超时未测完；按已测档幂律拟合（τ=a·n^{b:.2f}）外推约 "
                    f"{est / 3600:.2f} 小时（外推值，非实测）",
                    flush=True,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
