"""ontology-scale 三指标纯函数计算（口径唯一事实源；docs/Agent/16 §2 G 域冻结）。

纪律（agent-core metrics.py 同款）：只做确定性数值计算（输入=runner 采集的原始观测，
输出=结果 JSON 的 metrics 段），禁 IO、禁随机、禁读环境——同输入恒同输出。

指标 ↔ 红队审查问题映射（docs/评审/红队攻击性审查-2026-10-06）：
    validate_latency        ← G1 重型本体 SHACL 校验时延（含/不含违例两形态）
    assemble_token_cost     ← G1 三档装配组装成本（TBox 摘要 vs 全量 schema 两模式）
    reindex_consistency     ← F3 TBox 改版后旧实例漂移检出率
"""

from __future__ import annotations

import math
from statistics import fmean
from typing import Any

# ---------------------------------------------------------------------------
# ① validate_latency（G1：SHACL 校验时延曲线）
# ---------------------------------------------------------------------------


def validate_latency(
    clean_ms: list[float],
    violations_ms: list[float],
    *,
    violations_found: int,
    violations_expected: int,
) -> dict[str, Any]:
    """口径：pySHACL 校验时延（inference=none 档，平台 shacl.validate 同参）。

    干净 ABox（应 conforms=True）与带违例 ABox（应 conforms=False）两形态各采样
    repeats 次：p50/p95（nearest-rank）+ 均值 + 膨胀比（违例形态/干净形态——结果图
    构建的额外成本面）。violation_hit_exact=违例命中数与解析期望恰等（shapes 有效性的
    运行时证据；False=shapes 或数据漂移，时延数字随之存疑）。
    """
    return {
        "clean": _latency_block(clean_ms),
        "violations": _latency_block(violations_ms),
        "violation_overhead_ratio": _ratio(_mean_ms(violations_ms), _mean_ms(clean_ms)),
        "violations_found": violations_found,
        "violations_expected": violations_expected,
        "violation_hit_exact": violations_found == violations_expected,
    }


def _latency_block(samples_ms: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples_ms)
    return {
        "samples": len(ordered),
        "p50_ms": _percentile_nearest_rank(ordered, 50),
        "p95_ms": _percentile_nearest_rank(ordered, 95),
        "mean_ms": round(fmean(ordered), 3) if ordered else 0.0,
    }


def percentile_nearest_rank(values: list[float], pct: float) -> float:
    """nearest-rank 分位（rag metrics 同款语义）：ceil(pct/100·n) 序位值（1 起）；空集→0.0。"""
    return _percentile_nearest_rank(sorted(values), pct)


def _percentile_nearest_rank(ordered: list[float], pct: float) -> float:
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def _mean_ms(samples_ms: list[float]) -> float:
    return fmean(samples_ms) if samples_ms else 0.0


def _ratio(num: float, den: float) -> float | None:
    if den <= 0:
        return None
    return round(num / den, 4)


# ---------------------------------------------------------------------------
# ② assemble_token_cost（G1：TBox 组装进上下文的成本面）
# ---------------------------------------------------------------------------


def assemble_token_cost(
    summary_tokens: int,
    full_tokens: int,
    *,
    summary_chars: int,
    full_chars: int,
    class_count: int,
    counter_backend: str,
) -> dict[str, Any]:
    """口径：TBox 注入上下文的 token 成本——grounding 组装口径（ContextBlock.tokens 求和，
    services/agent/business/kernel/grounding.py:62 _estimated_tokens 同语义）。

    两模式（16 篇 §2 assemble_token_cost 三档）：
    - summary（类名+属性清单摘要）：grounding tier=1 稳定知识块的合成形态（摘要面）；
    - full（全量 schema：类公理+SHACL shapes Turtle）：整册直灌的对照面。
    compression_ratio = full/summary（>1=摘要面有净收益；=指标存在意义门槛）。
    counter_backend 随结果留档（vllm=真分词器 / heuristic=确定性估算——口径突变可归因）。
    """
    return {
        "summary_tokens": summary_tokens,
        "full_tokens": full_tokens,
        "summary_chars": summary_chars,
        "full_chars": full_chars,
        "compression_ratio": _ratio(float(full_tokens), float(summary_tokens)),
        "tokens_per_class_summary": _ratio(float(summary_tokens), float(class_count)),
        "tokens_per_class_full": _ratio(float(full_tokens), float(class_count)),
        "counter_backend": counter_backend,
    }


# ---------------------------------------------------------------------------
# ③ reindex_consistency（F3：TBox 改版 → 旧实例漂移检出率）
# ---------------------------------------------------------------------------


def reindex_consistency(records: list[dict[str, Any]], *, contract_kinds: tuple[str, ...]) -> dict[str, Any]:
    """口径：改 TBox/属性定义后，存量（旧）实例校验失败的检出能力。

    records = runner 逐变异采集：{kind, class_local, detected, violations_after}。
    - detection_rate = 检出变异数 / 契约型变异数（分母只计 shapes 契约三型——它们是
      F3「重索引漂移检出」的被测面；TBox-only 对照（rdfs:range 改动、shapes 不动）在
      inference=none 下 pySHACL 不读 rdfs:range，属**已知门禁盲区**，单独记
      tbox_only_visible 供归因，不计入分母（计入会把「盲区恒 0」与「检出能力」混一个数）。
    - per_kind_breakdown = 各型检出数/布靶数（哪型漏检一眼可见）。
    """
    contract = [r for r in records if r["kind"] in contract_kinds]
    tbox_only = [r for r in records if r["kind"] not in contract_kinds]
    detected = sum(1 for r in contract if r["detected"])
    breakdown: dict[str, dict[str, int]] = {}
    for record in contract:
        slot = breakdown.setdefault(record["kind"], {"targets": 0, "detected": 0})
        slot["targets"] += 1
        slot["detected"] += 1 if record["detected"] else 0
    return {
        "contract_mutations": len(contract),
        "contract_detected": detected,
        "detection_rate": round(detected / len(contract), 6) if contract else None,
        "per_kind_breakdown": breakdown,
        "tbox_only_visible": bool(tbox_only) and all(r["detected"] for r in tbox_only),
        "tbox_only_targets": len(tbox_only),
        "min_violations_after": min((r["violations_after"] for r in contract if r["detected"]), default=0),
    }
