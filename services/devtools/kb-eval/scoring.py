#!/usr/bin/env python3
"""U-③a Utopia 基准判分库（平级纯模块，kb-eval 独立脚本目录零包化；方案依据 docs/OntRAG/Utopia借鉴优化方案.md U-③a）。

设计约束（批次规格 2026-10-05，用户铁律：项目内不使用 mock 数据——本模块只判分不产数据）：
- **全局唯一判等函数** :func:`roughly`：数值放宽两个量级（相对容差 1e-2，「约 8.63 亿 == 862793473.48」级）；
  文本 NFKC+去空白+casefold 归一（poc2_calibration._norm 同源口径）；容器逐元素递归；
- **known_gap 机制** :class:`KnownGapLedger`：已登记缺口按 key 吸收失分项——剔除出全部分母、
  单列计数留痕（缺口显式化，不静默虚高分数）；
- **absent 单列**：期望有而系统无答（not_found 语义）不计 FP（无候选不惩罚精确率）、只计 FN，
  计数单列（tools/drawing-probe/eval_golden.py 宏平均口径同源）；
- **resolved/dangling 引用计数** :func:`count_references`：产出引用指向已知目标=resolved、
  悬空=dangling（知识完整性结构面指标）；
- 判分口径双参照：poc2_calibration.py 的 0.1 宽分桶 precision+ECE（:func:`calibration`）与
  eval_golden.py 的字段级 P/R/F1/宏平均（:func:`prf`/:func:`macro_average`）；
- 结果落盘沿用 *_results.json 惯例（:func:`save_results`）。

零第三方依赖（纯 stdlib）：脚本侧运行时同名目录自动入 sys.path 直接 ``import scoring``；
pytest 侧 importlib 按路径加载（tests/tools/test_eval_golden.py:20-24 先例）。
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable, Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from numbers import Real
from pathlib import Path
from typing import Any, Final, Literal

# 数值放宽两个量级：常规浮点判等 1e-4 量级 → 1e-2（约 8.63 亿 == 862793473.48，相对差 2.4e-4）
_REL_TOLERANCE: Final = 1e-2
_ABS_FLOOR: Final = 1e-9  # 近零区绝对下限（防除零与 0 vs 1e-12 类噪声）

Status = Literal["hit", "miss", "absent", "known_gap"]
_BIN_WIDTH: Final = 0.1  # 置信度分桶宽度（poc2 口径）
# 候选缺 confidence 的兜底值（poc2_calibration.CONF_MISSING 同名同值；供产数据侧 runner 引用）
CONF_MISSING: Final = 0.5


# ---------------------------------------------------------------- 全局唯一判等


def norm_text(value: str) -> str:
    """文本归一（poc2 _norm 同源）：NFKC（全角→半角）+ 去全部空白 + casefold。"""
    return "".join(unicodedata.normalize("NFKC", value or "").split()).casefold()


def roughly(actual: Any, expected: Any, *, rel: float = _REL_TOLERANCE, abs_floor: float = _ABS_FLOOR) -> bool:
    """全局唯一判等函数（本库一切比较经此口，禁旁路 ==）。

    - 双 None → True；单 None → False（absent 判定由调用方经 status 承载，不在此隐式吞）；
    - 数值（Real，非 bool）：``|a-e| ≤ max(abs_floor, rel × max(|a|,|e|))``——rel 缺省 1e-2
      即「约 8.63 亿 == 862793473.48」两个量级放宽；abs_floor 兜近零区；
    - 字符串：:func:`norm_text` 归一后相等；
    - dict：键集相等且逐 value 递归；list/tuple：等长且逐元素递归；
    - 其余（bool/None 混合型/异型）：回退 ``actual == expected``。
    """
    if actual is None and expected is None:
        return True
    if actual is None or expected is None:
        return False
    a_is_num = isinstance(actual, Real) and not isinstance(actual, bool)
    e_is_num = isinstance(expected, Real) and not isinstance(expected, bool)
    if a_is_num and e_is_num:
        a, e = float(actual), float(expected)
        return abs(a - e) <= max(abs_floor, rel * max(abs(a), abs(e)))
    if isinstance(actual, str) and isinstance(expected, str):
        return norm_text(actual) == norm_text(expected)
    if isinstance(actual, Mapping) and isinstance(expected, Mapping):
        return set(actual) == set(expected) and all(
            roughly(actual[k], expected[k], rel=rel, abs_floor=abs_floor) for k in actual
        )
    if isinstance(actual, (list, tuple)) and isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(
            roughly(a, e, rel=rel, abs_floor=abs_floor) for a, e in zip(actual, expected, strict=True)
        )
    return actual == expected


# ---------------------------------------------------------------- 逐项结果与 known_gap


@dataclass(frozen=True, slots=True)
class ScoredItem:
    """单项判分结果（status 四值；known_gap 项由 Ledger 吸收后重打标）。"""

    name: str
    status: Status
    detail: str = ""


@dataclass(frozen=True, slots=True)
class KnownGap:
    """已登记缺口（key 稳定可跨轮对比；reason 面向人）。"""

    key: str
    reason: str


class KnownGapLedger:
    """known_gap 账本：按 key 吸收失分项——剔除出全部分母、单列计数（不静默）。"""

    def __init__(self, gaps: Iterable[KnownGap] = ()) -> None:
        self._gaps: dict[str, KnownGap] = {}
        for gap in gaps:
            self.register(gap.key, gap.reason)

    def register(self, key: str, reason: str) -> None:
        """登记缺口（同 key 重复登记幂等，不覆盖既有 reason）。"""
        if key not in self._gaps:
            self._gaps[key] = KnownGap(key=key, reason=reason)

    def __contains__(self, key: object) -> bool:
        return key in self._gaps

    def absorb(
        self, items: Sequence[ScoredItem], key_of: Callable[[ScoredItem], str]
    ) -> tuple[list[ScoredItem], int]:
        """把命中缺口的项重打标 known_gap（项仍留在返回序列内，由 aggregate 剔出全部分母）；返回 (判分序列, 吸收数)。"""
        residual: list[ScoredItem] = []
        absorbed = 0
        for item in items:
            if key_of(item) in self:
                residual.append(ScoredItem(item.name, "known_gap", f"known_gap 吸收：{item.detail}"))
                absorbed += 1
            else:
                residual.append(item)
        return residual, absorbed

    def as_list(self) -> list[dict[str, str]]:
        """登记清单（落盘/台账用）。"""
        return [{"key": g.key, "reason": g.reason} for g in self._gaps.values()]


# ---------------------------------------------------------------- 指标（eval_golden 口径）


def prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    """P/R/F1（零分母 → 0.0，不炸；公式同 eval_golden.doc_metrics）。"""
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def aggregate(items: Sequence[ScoredItem]) -> dict[str, Any]:
    """逐项结果 → 汇总计数 + P/R/F1（absent 单列口径）。

    - precision = hit/(hit+miss)：absent 无产出不惩罚精确率；
    - recall = hit/(hit+miss+absent)：期望有而未答仍是漏报；
    - known_gap：剔除出全部分母，仅单列计数（缺口显式化）；
    - 零分母 → 0.0（不炸）。
    """
    counts = {"hit": 0, "miss": 0, "absent": 0, "known_gap": 0}
    for item in items:
        counts[item.status] += 1
    scores = prf(counts["hit"], counts["miss"], counts["miss"] + counts["absent"])
    return {
        "hit": counts["hit"],
        "miss": counts["miss"],
        "absent": counts["absent"],
        "known_gap": counts["known_gap"],
        "total": len(items),
        **scores,
    }


def macro_average(groups: Sequence[Mapping[str, float]]) -> dict[str, float]:
    """组级指标宏平均（仅计入有效组——absent-only/not_found 组由调用方排除，eval_golden 口径）。"""
    keys = ("precision", "recall", "f1")
    if not groups:
        return dict.fromkeys(keys, 0.0)
    return {k: sum(g[k] for g in groups) / len(groups) for k in keys}  # type: ignore[misc]


# ---------------------------------------------------------------- 置信度校准（poc2 口径）


def calibration(
    pairs: Sequence[tuple[float, bool]], *, bin_width: float = _BIN_WIDTH, conf_missing: bool = False
) -> dict[str, Any]:
    """0.1 宽分桶 precision + ECE（poc2_calibration.calibrate 同源口径，纯函数化）。

    ``pairs`` = (自报置信度, 是否命中正类)；置信度截到 [0,1]（poc2 仅截桶下标，本库连值一起截，
    防越界值抬高 avg_confidence）；``conf_missing``=本批含缺失置信度（调用方以 CONF_MISSING
    兜底传入时置 True 单列）。ECE=Σ (桶占比 × |桶精确率 − 桶均置信度|)。
    """
    n_bins = round(1.0 / bin_width)
    bins: list[dict[str, Any]] = [
        {"bin": f"{i * bin_width:.1f}-{(i + 1) * bin_width:.1f}", "total": 0, "correct": 0, "conf_sum": 0.0}
        for i in range(n_bins)
    ]
    for conf, correct in pairs:
        conf = min(1.0, max(0.0, float(conf)))
        idx = min(n_bins - 1, max(0, int(conf / bin_width)))
        bins[idx]["total"] += 1
        bins[idx]["correct"] += int(bool(correct))
        bins[idx]["conf_sum"] += float(conf)
    total = len(pairs)
    for b in bins:
        b["precision"] = round(b["correct"] / b["total"], 4) if b["total"] else None
        b["avg_confidence"] = round(b["conf_sum"] / b["total"], 4) if b["total"] else None
    ece = sum(
        (b["total"] / total) * abs(b["precision"] - b["avg_confidence"])
        for b in bins
        if b["total"] and b["precision"] is not None and b["avg_confidence"] is not None
    )
    return {
        "total": total,
        "confidence_missing": int(conf_missing),
        "bins": [{k: v for k, v in b.items() if k != "conf_sum"} for b in bins],
        "ece": round(ece, 4),
        "true_positives": sum(b["correct"] for b in bins),
        "false_positives": total - sum(b["correct"] for b in bins),
    }


# ---------------------------------------------------------------- 引用计数（resolved/dangling）


@dataclass(frozen=True, slots=True)
class RefCounts:
    """产出引用计数：resolved=指向已知目标；dangling=悬空（指向不存在目标）。"""

    total: int
    resolved: int
    dangling: int
    dangling_refs: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "resolved": self.resolved,
            "dangling": self.dangling,
            "dangling_refs": list(self.dangling_refs),
        }


def count_references(produced: Iterable[str], targets: Container[str]) -> RefCounts:
    """产出引用计数（唯一引用视角：重复引用去重只计一次）。

    ``targets`` 为已知目标集合（如全部 chunk_id/document_id）；命中=resolved，未命中=dangling
    （悬空引用逐条留痕供追查）。
    """
    unique = list(dict.fromkeys(str(r) for r in produced))
    dangling = tuple(r for r in unique if r not in targets)
    resolved = len(unique) - len(dangling)
    return RefCounts(total=len(unique), resolved=resolved, dangling=len(dangling), dangling_refs=dangling)


# ---------------------------------------------------------------- 落盘（*_results.json 惯例）


def save_results(path: Path, payload: Mapping[str, Any]) -> Path:
    """结果落盘：ensure_ascii=False + indent=2 + utf-8；返回落盘路径。"""
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
