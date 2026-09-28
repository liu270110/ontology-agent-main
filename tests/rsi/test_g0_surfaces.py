# tests/rsi/test_g0_surfaces.py
"""进化面注册表用例（architecture/09 §13.2 封闭八面 + §13.5 rsi:EvolutionSurface）。

断言目标：
- 八面封闭：REGISTRY 与枚举一一对应、缺一不可多一不可（新增面=代码变更=人工审批）；
- 四元组元数据逐字对齐 §13.2（O1 工具实现锚点/触发信号/产物/判据抽查 + 全量非空）；
- surface_of 大小写不敏感查询、未局面名拒绝（不存在动态面）；
- TriggerTrack.GAP 枚举位（§13.4 三轨）与触发注册面对 GAP 的封闭。
"""

from __future__ import annotations

import pytest

from services.rsi.audit import InMemoryAuditTrail
from services.rsi.proposal import TriggerTrack
from services.rsi.surfaces import REGISTRY, EvolutionSurface, SurfaceMeta, surface_of
from services.rsi.triggers import TriggerRegistry


def test_进化面封闭八面_注册表与枚举一一对应() -> None:
    assert {s.value for s in EvolutionSurface} == {"O1", "O2", "O3", "O4", "O5", "O6", "O7", "O8"}
    assert set(REGISTRY) == set(EvolutionSurface)
    assert len(REGISTRY) == 8
    assert all(isinstance(meta, SurfaceMeta) for meta in REGISTRY.values())


def test_四元组元数据逐字对齐_13_2八行表() -> None:
    o1 = REGISTRY[EvolutionSurface.TOOL_IMPL]
    assert o1.name == "工具实现"
    assert "ob2:Action" in o1.ontology_anchor and "实现绑定" in o1.ontology_anchor
    assert "行动类缺绑定" in o1.trigger_signals  # G0 unbound_action 的触发信号出处
    assert "工具集" in o1.artifact and "行动类语义标注" in o1.artifact
    assert "成功率" in o1.criterion
    o2 = REGISTRY[EvolutionSurface.ACTION_SEMANTICS]
    assert o2.name == "行动类语义" and "unmapped intent" in o2.trigger_signals
    assert "changeset" in o2.artifact and "人工终审" in o2.artifact  # 语义变更必走人工终审
    o8 = REGISTRY[EvolutionSurface.MODEL_WEIGHT]
    assert o8.name == "模型权重" and "holdout" in o8.criterion


def test_元数据四元组全量非空() -> None:
    for surface, meta in REGISTRY.items():
        assert meta.name, surface
        assert meta.ontology_anchor and meta.trigger_signals and meta.artifact and meta.criterion


def test_surface_of查询_大小写不敏感_未面拒绝() -> None:
    assert surface_of("O1").name == "工具实现"
    assert surface_of("o5").name == "控制流程"
    assert surface_of(" O8 ").name == "模型权重"
    with pytest.raises(KeyError):
        surface_of("O9")  # 封闭注册表：不存在动态面（扩面=代码变更=人工审批）


async def test_GAP轨枚举位_注册面封闭() -> None:
    assert TriggerTrack.GAP.value == "gap"
    registry = TriggerRegistry(audit_trail=InMemoryAuditTrail())

    async def handler(event: object) -> None:  # pragma: no cover — 不应被注册成功
        return None

    with pytest.raises(ValueError, match="缺口轨不经触发注册面驱动"):
        registry.register(TriggerTrack.GAP, "gap.detector", handler)  # type: ignore[arg-type]
    assert registry.registered() == []
