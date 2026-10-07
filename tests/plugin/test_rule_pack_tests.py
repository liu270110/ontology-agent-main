# tests/plugin/test_rule_pack_tests.py
"""K30-c 规则包自测字段单测（方案依据=docs/Agent/13 §36；上游=codex §12 execpolicy
not_match 先跑自测语义的清单级落地）。

覆盖：manifest 门禁 1 对 rule_packs[].tests 的格式校验（合法通过/非法 4503/缺省零行为
变化）+ 门禁 6 对同字段的 findings 拦截与声明计数留痕。v1=格式校验（执行需规则 pattern
本体，随制品级校验批次——manifest.rule_pack_tests_problems docstring 取舍说明）。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from services.platform.kernel import DomainError
from services.plugin.business.gates import GateContext, gate_behavior_fixture
from services.plugin.domain.model.manifest import validate_manifest
from tests.plugin.helpers import CHECKSUM, VALID_SERVER_JSON


def _manifest() -> dict[str, Any]:
    return copy.deepcopy(VALID_SERVER_JSON)


def _ctx() -> GateContext:
    return GateContext(artifact_key=f"plugin-packages/x/{CHECKSUM[:8]}/package.zip", checksum=CHECKSUM)


def _pack(**overrides: Any) -> dict[str, Any]:
    pack: dict[str, Any] = {
        "name": "pack.r1",
        "artifact": "rules/r.ttl",
        "mount": ["gates.pre"],
        "default_enabled": False,
        "semantic_annotation": {"rule_iris": ["http://o#R1"]},
    }
    pack.update(overrides)
    return pack


# ---------------------------------------------------------------- 门禁 1：manifest schema


def test_门禁1_规则包tests合法样本_通过且零行为变化():
    server_json = _manifest()
    server_json["x-platform"]["rule_packs"] = [_pack(tests=[{"match": "停电超过 2 小时"}, {"not_match": "计划内检修"}])]
    report = validate_manifest(server_json)  # 不抛即通过
    assert report["passed"] is True


def test_门禁1_缺省无tests键_零行为变化():
    server_json = _manifest()
    server_json["x-platform"]["rule_packs"] = [_pack()]  # 无 tests 键
    report = validate_manifest(server_json)
    assert report["passed"] is True


@pytest.mark.parametrize(
    "tests",
    [
        [],  # 空数组（非空样本要求）
        "not-a-list",  # 非数组
        [{"match": "a", "not_match": "b"}],  # 双键（恰含其一）
        [{"foo": "bar"}],  # 两键皆缺
        [{"match": ""}],  # 空串样本
        [{"match": 1}],  # 非字符串样本
    ],
)
def test_门禁1_规则包tests非法格式_4503拒(tests):
    server_json = _manifest()
    server_json["x-platform"]["rule_packs"] = [_pack(tests=tests)]
    with pytest.raises(DomainError, match="4503") as excinfo:
        validate_manifest(server_json)
    assert "rule_packs[0].tests" in str(excinfo.value)


# ---------------------------------------------------------------- 门禁 6：行为符合性夹具


def test_门禁6_规则包tests非法_findings拦截():
    server_json = _manifest()
    server_json["package_type"] = "capability_pack"
    server_json["x-platform"]["rule_packs"] = [_pack(tests=[{"match": "a", "not_match": "b"}])]
    result = gate_behavior_fixture(server_json, _ctx())
    assert result.passed is False
    assert any("rule_packs[0].tests" in f for f in result.findings)


def test_门禁6_规则包tests合法_通过且声明计数留痕():
    server_json = _manifest()
    server_json["package_type"] = "capability_pack"
    server_json["x-platform"]["rule_packs"] = [
        _pack(tests=[{"match": "x"}, {"not_match": "y"}]),
        _pack(name="pack.r2", artifact="rules/r2.ttl"),  # 无 tests → 不计数
    ]
    result = gate_behavior_fixture(server_json, _ctx())
    assert result.passed is True
    assert result.details["rule_packs_tests_declared"] == 1  # 声明留痕（v1 格式校验口径）
