"""intent v0 金标集单测：schema 契约（数量/分布/词表/期望一致性）+ 生成器零漂移。"""

from __future__ import annotations

import pytest

from benchmarks.suites.intent.action_catalog import build_action_catalog
from benchmarks.suites.intent.dataset import load_dataset
from benchmarks.suites.intent.datasets.build_golden_v0 import (
    EXPECTED_DISTRIBUTION,
    build_items,
    dataset_sha256,
    validate,
)


class TestGoldenV0Schema:
    """v0 金标骨架契约：100 条 × 四分布 40/30/20/10（docs/Agent/16 §2 intent）。"""

    def test_金标数量与四层分布(self):
        # Arrange / Act
        items = load_dataset("v0")
        # Assert
        assert len(items) == 100
        dist: dict[str, int] = {}
        for it in items:
            dist[it.ambiguity_level] = dist.get(it.ambiguity_level, 0) + 1
        assert dist == EXPECTED_DISTRIBUTION
        assert dist == {"clear": 40, "ambiguous": 30, "need_clarification": 20, "out_of_scope": 10}

    def test_期望与层级一一对应(self):
        # Arrange / Act
        items = load_dataset("v0")
        # Assert：map↔clear/ambiguous、clarify↔need_clarification、reject↔out_of_scope
        for it in items:
            if it.ambiguity_level in ("clear", "ambiguous"):
                assert it.expectation == "map"
            elif it.ambiguity_level == "need_clarification":
                assert it.expectation == "clarify"
                assert it.expected_action == "ask_user"  # 澄清=平台真实行动类
            else:
                assert it.expectation == "reject"
                assert it.expected_action is None  # 越界条无合法行动类

    def test_映射条金标全部落在平台行动类词表(self):
        # Arrange
        catalog_names = {e.name for e in build_action_catalog()}
        # Act
        items = load_dataset("v0")
        # Assert：非越界条的 expected_action 必为 17 静态行动类之一
        mapped = {it.expected_action for it in items if it.expected_action is not None}
        assert mapped <= catalog_names
        # 映射类覆盖面：17 类中至少 16 类作为金标出现（词表全量可见于 prompt 才有判分意义）
        assert len(mapped) >= 16

    def test_id_连续唯一且字段非空(self):
        # Arrange / Act
        items = load_dataset("v0")
        # Assert
        assert [it.id for it in items] == [f"intent-{i:03d}" for i in range(1, 101)]
        assert all(it.query.strip() and it.notes.strip() for it in items)


class TestGeneratorConsistency:
    """生成器与入库 JSONL 零漂移：重跑 build 逻辑应与落盘文件逐条等价（确定性说明的锁定面）。"""

    def test_再生成与落盘一致(self):
        # Arrange：读入库文件
        disk = load_dataset("v0")
        # Act：内存再生成 + 校验
        gen = build_items()
        validate(gen, valid_actions={e.name for e in build_action_catalog()})
        # Assert：逐条相等（生成器=落盘唯一来源）
        assert [(it.id, it.query, it.expected_action, it.ambiguity_level, it.notes, it.expectation) for it in disk] == [
            (g["id"], g["query"], g["expected_action"], g["ambiguity_level"], g["notes"], g["expectation"]) for g in gen
        ]

    def test_指纹稳定(self):
        # Arrange / Act
        once = dataset_sha256(build_items())
        twice = dataset_sha256(build_items())
        # Assert：零随机——重跑指纹逐字节一致
        assert once == twice
        assert len(once) == 64

    def test_validate_拒绝坏分布(self):
        # Arrange：抽掉一条 clear 破坏 40 分布
        items = build_items()
        broken = [it for it in items if not (it["ambiguity_level"] == "clear" and it["id"] == "intent-001")]
        # Act / Assert
        with pytest.raises(ValueError, match="分布漂移|金标须"):
            validate(broken, valid_actions={e.name for e in build_action_catalog()})

    def test_validate_拒绝词表外金标(self):
        # Arrange：映射条金标改成不存在的行动类
        items = build_items()
        items[0]["expected_action"] = "nonexistent_action"
        # Act / Assert
        with pytest.raises(ValueError, match="不在平台行动类词表"):
            validate(items, valid_actions={e.name for e in build_action_catalog()})
