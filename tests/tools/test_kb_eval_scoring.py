"""U-③a 判分库单测（纯函数直接断言期望值，零 IO 零模型）。

脚本本体 services/devtools/kb-eval/scoring.py（kb-eval 独立脚本目录非包，经 importlib 按路径加载，
tests/tools/test_eval_golden.py:20-24 先例）。锁口径：全局唯一判等 roughly（数值放宽两个量级）/
known_gap 吸收 / absent 单列 / 0.1 宽分桶 precision+ECE / resolved-dangling 引用计数 / 落盘惯例。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "services" / "devtools" / "kb-eval" / "scoring.py"

_spec = importlib.util.spec_from_file_location("kb_eval_scoring_under_test", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
scoring = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("kb_eval_scoring_under_test", scoring)
_spec.loader.exec_module(scoring)


# ---------------------------------------------------------------- roughly（全局唯一判等）


def test_roughly_数值放宽两个量级_亿级案例():
    # 规格锚点案例：约 8.63 亿 == 862793473.48（相对差 2.4e-4 ≪ 1e-2）
    assert scoring.roughly(863_000_000, 862793473.48)
    assert scoring.roughly(862793473.48, 863_000_000)  # 对称
    # 边界内（差 8e6 / 8.63e8 ≈ 9.3e-3 < 1e-2）
    assert scoring.roughly(863_000_000, 855_000_000)
    # 边界外（差 9e6 / 8.63e8 ≈ 1.04e-2 > 1e-2）
    assert not scoring.roughly(863_000_000, 854_000_000)


def test_roughly_近零区绝对下限():
    assert scoring.roughly(0.0, 1e-10)  # abs_floor 兜近零噪声
    assert not scoring.roughly(0.0, 1e-6)  # 超出 abs_floor 且 rel×max 亦不够


def test_roughly_文本归一口径():
    assert scoring.roughly("１０ｋＶ 滨河线", "10kv滨河线")  # NFKC+去空白+casefold
    assert scoring.roughly(" Feeder F001 ", "feederf001")
    assert not scoring.roughly("滨河线", "城网线")


def test_roughly_None语义():
    assert scoring.roughly(None, None)
    assert not scoring.roughly(None, 0)
    assert not scoring.roughly(0, None)


def test_roughly_容器递归():
    assert scoring.roughly({"a": 1.0, "b": ["x"]}, {"a": 1.005, "b": ["x"]})  # 数值放宽逐值生效
    assert not scoring.roughly({"a": 1.0}, {"a": 1.0, "b": 2})  # 键集不同
    assert scoring.roughly((1, 2), [1, 2])  # list/tuple 互通
    assert not scoring.roughly([1, 2], [1, 2, 3])  # 等长约束
    assert not scoring.roughly(1, "1")  # 异型不硬凑


# ---------------------------------------------------------------- KnownGapLedger（known_gap 机制）


def test_ledger_登记_幂等_包含():
    ledger = scoring.KnownGapLedger()
    ledger.register("kg-align-llm", "对齐 LLM 判定不可用，二级嵌入跳过")
    ledger.register("kg-align-llm", "重复登记被幂等吸收")
    assert "kg-align-llm" in ledger
    assert "kg-other" not in ledger
    assert ledger.as_list() == [{"key": "kg-align-llm", "reason": "对齐 LLM 判定不可用，二级嵌入跳过"}]


def test_ledger_absorb_命中缺口重打标_计数单列():
    ledger = scoring.KnownGapLedger()
    ledger.register("c", "embedder 未装配（该名候选整项吸收）")
    items = [
        scoring.ScoredItem("a", "hit"),
        scoring.ScoredItem("b", "miss"),
        scoring.ScoredItem("c", "miss"),  # name 命中缺口
    ]
    residual, absorbed = ledger.absorb(items, key_of=lambda i: i.name)
    assert absorbed == 1
    assert [i.status for i in residual] == ["hit", "miss", "known_gap"]
    summary = scoring.aggregate(residual)
    assert summary["known_gap"] == 1  # 单列
    assert (summary["hit"], summary["miss"]) == (1, 1)  # 分母剔除缺口项
    assert summary["precision"] == pytest.approx(0.5)
    assert summary["recall"] == pytest.approx(0.5)  # 1/(1+1+0)——known_gap 项不进分母


# ---------------------------------------------------------------- aggregate（absent 单列口径）


def test_aggregate_四态计数与手算指标():
    items = [
        scoring.ScoredItem("a", "hit"),
        scoring.ScoredItem("b", "hit"),
        scoring.ScoredItem("c", "miss"),
        scoring.ScoredItem("d", "absent"),
        scoring.ScoredItem("e", "known_gap"),
    ]
    s = scoring.aggregate(items)
    assert (s["hit"], s["miss"], s["absent"], s["known_gap"], s["total"]) == (2, 1, 1, 1, 5)
    assert s["precision"] == pytest.approx(2 / 3)  # absent 不进 P 分母
    assert s["recall"] == pytest.approx(2 / 4)  # absent 计 FN
    assert s["f1"] == pytest.approx(4 / 7)  # 调和均值手算


def test_aggregate_空与全absent_零不炸():
    empty = scoring.aggregate([])
    assert empty["total"] == 0 and empty["precision"] == 0.0 and empty["recall"] == 0.0 and empty["f1"] == 0.0
    all_absent = scoring.aggregate([scoring.ScoredItem("x", "absent")])
    assert all_absent["precision"] == 0.0 and all_absent["recall"] == 0.0 and all_absent["absent"] == 1


def test_prf_手算与零分母():
    p = scoring.prf(2, 1, 1)
    assert p["precision"] == pytest.approx(2 / 3)
    assert p["recall"] == pytest.approx(2 / 3)
    assert p["f1"] == pytest.approx(2 / 3)
    assert scoring.prf(0, 0, 0) == {"precision": 0.0, "recall": 0.0, "f1": 0.0}


def test_macro_average_组均值与空组():
    g1 = scoring.prf(2, 0, 0)  # 全对
    g2 = scoring.prf(1, 1, 1)
    mean = scoring.macro_average([g1, g2])
    assert mean["precision"] == pytest.approx((1.0 + 0.5) / 2)
    assert mean["recall"] == pytest.approx((1.0 + 0.5) / 2)
    assert scoring.macro_average([]) == {"precision": 0.0, "recall": 0.0, "f1": 0.0}


# ---------------------------------------------------------------- calibration（poc2 口径）


def test_calibration_手算分桶与ECE():
    report = scoring.calibration([(0.9, True), (0.95, False), (0.2, False)])
    assert report["total"] == 3
    assert (report["true_positives"], report["false_positives"]) == (1, 2)
    bin9 = report["bins"][9]
    assert (bin9["bin"], bin9["total"], bin9["correct"]) == ("0.9-1.0", 2, 1)
    assert bin9["precision"] == pytest.approx(0.5)
    assert bin9["avg_confidence"] == pytest.approx(0.925)
    bin2 = report["bins"][2]
    assert (bin2["bin"], bin2["total"], bin2["precision"]) == ("0.2-0.3", 1, 0.0)
    # ECE = (2/3)·|0.5−0.925| + (1/3)·|0.0−0.2| = 0.35
    assert report["ece"] == pytest.approx(0.35, abs=1e-4)


def test_calibration_越界置信度截到01():
    report = scoring.calibration([(1.7, True), (-0.3, True)])
    assert report["bins"][9]["avg_confidence"] == pytest.approx(1.0)  # 1.7 截为 1.0
    assert report["bins"][0]["avg_confidence"] == pytest.approx(0.0)  # -0.3 截为 0.0
    # ECE = 0.5·|1.0−1.0| + 0.5·|1.0−0.0| = 0.5
    assert report["ece"] == pytest.approx(0.5, abs=1e-4)


def test_calibration_空批_零不炸():
    report = scoring.calibration([])
    assert report["total"] == 0 and report["ece"] == 0 and all(b["precision"] is None for b in report["bins"])


# ---------------------------------------------------------------- count_references（resolved/dangling）


def test_count_references_去重_悬空留痕():
    counts = scoring.count_references(["c1", "c2", "c1", "ghost"], {"c1", "c2"})
    assert counts.total == 3  # 重复引用只计一次
    assert counts.resolved == 2
    assert counts.dangling == 1
    assert counts.dangling_refs == ("ghost",)
    assert counts.as_dict() == {"total": 3, "resolved": 2, "dangling": 1, "dangling_refs": ["ghost"]}


def test_count_references_空产出():
    counts = scoring.count_references([], {"c1"})
    assert (counts.total, counts.resolved, counts.dangling) == (0, 0, 0)


# ---------------------------------------------------------------- save_results（*_results.json 惯例）


def test_save_results_utf8中文roundtrip(tmp_path):
    out = tmp_path / "u3a_results.json"
    payload = {"round": 1, "note": "中文不转义", "bins": [{"bin": "0.9-1.0"}]}
    written = scoring.save_results(out, payload)
    assert written == out
    assert json.loads(out.read_text(encoding="utf-8")) == payload
    assert "中文不转义" in out.read_text(encoding="utf-8")  # ensure_ascii=False 口径
