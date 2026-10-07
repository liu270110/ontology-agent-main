"""ontology-scale runner 断言面单测：白名单纪律 + 断言求值语义（agent-core 同款）。"""

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


runner = _load("bench_test_onto_scale_runner", "runner.py")


class TestBenchmarkAsserts:
    """基准断言面：白名单键 + 求值语义（缺失/失败不静默）。"""

    def test_基准断言键全部在白名单内(self):
        # Act
        asserts = runner.benchmark_asserts()
        # Assert：防作弊断言纪律——断言键 ⊆ metrics 白名单
        assert {a["metric"] for a in asserts} <= runner._ALLOWED_METRIC_KEYS
        assert {a["metric"] for a in asserts} == {
            "violation_hit_exact",
            "clean_conforms",
            "violations_nonconforms",
            "detection_rate",
            "compression_ratio",
        }

    def test_求值_全过与缺键失败(self):
        # Arrange：达标 metrics vs 缺键 metrics
        good = {
            "violation_hit_exact": True,
            "clean_conforms": True,
            "violations_nonconforms": True,
            "detection_rate": 1.0,
            "compression_ratio": 3.8,
        }
        missing = {k: v for k, v in good.items() if k != "detection_rate"}
        # Act
        passed = runner.eval_asserts(runner.benchmark_asserts(), good)
        failed = runner.eval_asserts(runner.benchmark_asserts(), missing)
        # Assert：全过；缺键=失败并注明（actual=None 不静默）
        assert all(item["passed"] for item in passed)
        assert any(item["metric"] == "detection_rate" and not item["passed"] for item in failed)

    def test_求值_检出率漏检即断言失败(self):
        # Arrange：detection_rate=2/3（漏检如实落 assert_failed）
        metrics_out = {
            "violation_hit_exact": True,
            "clean_conforms": True,
            "violations_nonconforms": True,
            "detection_rate": 0.666667,
            "compression_ratio": 3.8,
        }
        # Act
        results = runner.eval_asserts(runner.benchmark_asserts(), metrics_out)
        # Assert
        assert any(item["metric"] == "detection_rate" and not item["passed"] for item in results)


class TestCountUnitsBackend:
    """token 计数后端：heuristic 确定性；未知后端拒绝（不静默降级）。"""

    def test_heuristic确定性且口径可解释(self):
        # Arrange：CJK×1 + ASCII 词×1（rag metrics.estimate_tokens 同语义）
        import asyncio

        text = ["pw:Transformer00000｜配电变压器｜属性: ratedVoltageKv"]
        # Act
        once = asyncio.run(runner.count_units(text, backend="heuristic"))
        again = asyncio.run(runner.count_units(text, backend="heuristic"))
        # Assert
        assert once == again and once > 0

    def test_未知后端拒绝(self):
        # Act / Assert
        import asyncio

        try:
            asyncio.run(runner.count_units(["x"], backend="no_such_backend"))
        except ValueError as exc:
            assert "未知 token_counter" in str(exc)
        else:
            raise AssertionError("未知后端必须 ValueError 拒绝")
