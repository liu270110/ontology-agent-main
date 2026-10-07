"""ontology-scale 指标纯函数单测：口径冻结（docs/Agent/16 §2）+ 确定性 + 边界。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SIBLING = Path(__file__).resolve().parents[2] / "benchmarks" / "suites" / "ontology-scale"


def _load(module_name: str, file_name: str):
    spec = importlib.util.spec_from_file_location(module_name, str(_SIBLING / file_name))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


metrics = _load("bench_test_onto_scale_metrics", "metrics.py")


class TestValidateLatency:
    """① validate_latency 口径：两形态分位 + 膨胀比 + 命中恰等标志。"""

    def test_分位与膨胀比(self):
        # Arrange：干净 [100,200,300,400]、违例 [200,400,600,800]（ms）
        # Act
        out = metrics.validate_latency(
            [100.0, 200.0, 300.0, 400.0],
            [200.0, 400.0, 600.0, 800.0],
            violations_found=10,
            violations_expected=10,
        )
        # Assert：nearest-rank p50=序位2、p95=序位4；膨胀比=均值比
        assert out["clean"]["p50_ms"] == 200.0
        assert out["clean"]["p95_ms"] == 400.0
        assert out["violations"]["p50_ms"] == 400.0
        assert out["violations"]["p95_ms"] == 800.0
        assert out["violation_overhead_ratio"] == 2.0
        assert out["violation_hit_exact"] is True

    def test_命中数漂移即_flag假(self):
        # Act
        out = metrics.validate_latency([100.0], [150.0], violations_found=7, violations_expected=10)
        # Assert：命中数≠期望 → shapes/数据漂移信号
        assert out["violation_hit_exact"] is False

    def test_空采样边界(self):
        # Act / Assert：空集分位=0、膨胀比=None（不伪造数字）
        out = metrics.validate_latency([], [], violations_found=0, violations_expected=0)
        assert out["clean"]["p50_ms"] == 0.0
        assert out["clean"]["samples"] == 0
        assert out["violation_overhead_ratio"] is None

    def test_纯函数确定性(self):
        # Arrange
        args = ([100.0, 300.0], [150.0, 450.0])
        # Act / Assert：同输入恒同输出
        assert metrics.validate_latency(*args, violations_found=1, violations_expected=1) == metrics.validate_latency(
            *args, violations_found=1, violations_expected=1
        )


class TestAssembleTokenCost:
    """② assemble_token_cost 口径：两模式 token + 压缩比 + 计数后端留档。"""

    def test_压缩比与单类成本(self):
        # Act：摘要 1000 tok vs 全量 4000 tok（100 类）
        out = metrics.assemble_token_cost(
            1000, 4000, summary_chars=5000, full_chars=20000, class_count=100, counter_backend="vllm"
        )
        # Assert
        assert out["compression_ratio"] == 4.0
        assert out["tokens_per_class_summary"] == 10.0
        assert out["tokens_per_class_full"] == 40.0
        assert out["counter_backend"] == "vllm"

    def test_摘要无收益时比值不粉饰(self):
        # Act：全量 ≤ 摘要（病态输入）
        out = metrics.assemble_token_cost(
            500, 400, summary_chars=1, full_chars=1, class_count=10, counter_backend="vllm"
        )
        # Assert：比值 <1 照实出数（asserts 层判失败，指标层不藏）
        assert out["compression_ratio"] == 0.8


class TestReindexConsistency:
    """③ reindex_consistency 口径：检出率分母只计契约型，TBox-only 盲区单列。"""

    def test_全检出与盲区单列(self):
        # Arrange：三契约型全检出 + TBox-only 未检出（inference=none 预期）
        records = [
            {"kind": "range_tighten", "detected": True, "violations_after": 10},
            {"kind": "enum_narrow", "detected": True, "violations_after": 10},
            {"kind": "required_add", "detected": True, "violations_after": 10},
            {"kind": "tbox_range_only", "detected": False, "violations_after": 0},
        ]
        # Act
        out = metrics.reindex_consistency(records, contract_kinds=("range_tighten", "enum_narrow", "required_add"))
        # Assert：检出率=3/3=1.0；盲区单列不计分母
        assert out["detection_rate"] == 1.0
        assert out["contract_detected"] == 3
        assert out["tbox_only_visible"] is False
        assert out["tbox_only_targets"] == 1
        assert out["per_kind_breakdown"]["range_tighten"] == {"targets": 1, "detected": 1}

    def test_漏检如实降率(self):
        # Arrange：enum_narrow 漏检
        records = [
            {"kind": "range_tighten", "detected": True, "violations_after": 5},
            {"kind": "enum_narrow", "detected": False, "violations_after": 0},
            {"kind": "required_add", "detected": True, "violations_after": 3},
        ]
        # Act
        out = metrics.reindex_consistency(records, contract_kinds=("range_tighten", "enum_narrow", "required_add"))
        # Assert
        assert out["detection_rate"] == round(2 / 3, 6)
        assert out["min_violations_after"] == 3

    def test_空记录边界(self):
        # Act / Assert：无契约记录 → rate=None（不伪造 0 或 1）
        out = metrics.reindex_consistency([], contract_kinds=("range_tighten",))
        assert out["detection_rate"] is None
