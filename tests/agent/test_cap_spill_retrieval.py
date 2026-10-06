# tests/agent/test_cap_spill_retrieval.py
"""spill 兑换能力测试（docs/Agent/13 §20 K14，headroom §5 CCR 闭环读侧收口）。

覆盖：K14-a SpillStore.get 租户隔离（同租户取回/拒跨租户/拒路径逃逸/读不到=None 双
语义）；K14-b spill.get 兑换工具端到端（落盘→分窗续读/读不到恢复指引/非法 locator
拒绝/只读免审批+limit 硬钳+参数纵深防御）；K14-c 截断标记同源（内核预览内嵌 locator）
与组合根 spill 装配门（None=兑换工具不注册）。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from services.agent.api.sessions import _build_spill_store
from services.agent.business.capabilities.spill_retrieval import (
    RETRIEVE_DEFAULT_LIMIT_CHARS,
    RETRIEVE_MAX_LIMIT_CHARS,
    SPILL_GET_ACTION_IRI,
    build_spill_retrieval_binding,
)
from services.agent.business.kernel.spill import (
    SPILL_MARKER_FMT,
    SPILL_THRESHOLD_CHARS,
    build_preview_payload,
    spill_if_oversized,
)
from services.agent.data.spill_store import LocalDirSpillStore
from services.agent.domain.model.kernel_actions import ExecutionMode, ToolCall, ToolResult
from services.agent.domain.model.kernel_context import TenantContext
from services.platform.config import Settings
from services.platform.errors import ErrorCode

# ── 构造器 ─────────────────────────────────────────────────────────────────────


def _ctx(tenant: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(tenant_id=tenant or uuid.uuid4(), trace_id="trace-spill-retrieval-test")


def _call(parameters: dict[str, Any]) -> ToolCall:
    return ToolCall(
        action_iri=SPILL_GET_ACTION_IRI,
        execution_mode=ExecutionMode.READ,
        parameters=parameters,
        param_hash="test-hash",
    )


# ── K14-a：SpillStore.get 租户隔离 ────────────────────────────────────────────


async def test_get_同租户原文可取回_跨租户与越界拒绝_读不到返回None(tmp_path: Path):
    # arrange：tenant_a 落盘一份原文
    store = LocalDirSpillStore(tmp_path)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    locator = await store.put(f"spill/{tenant_a}/run-1/call-1.json", "原文ABC")
    # act + assert：同租户兑换成功
    assert await store.get(locator, tenant_id=str(tenant_a)) == "原文ABC"
    # 跨租户拒绝（租户归属校验：root 内但非本租户段）
    with pytest.raises(ValueError):
        await store.get(locator, tenant_id=str(tenant_b))
    # 路径逃逸拒绝（与 put 同款防护）
    outside = tmp_path.parent / "escape.txt"
    outside.write_text("越界内容", encoding="utf-8")
    with pytest.raises(ValueError):
        await store.get(str(outside), tenant_id=str(tenant_a))
    # 读不到 ≠ 非法：root 内同租户但文件不存在 → None（双语义可区分）
    missing = str(tmp_path / f"spill/{tenant_a}/run-1/missing.json")
    assert await store.get(missing, tenant_id=str(tenant_a)) is None


async def test_get_空locator_结构化拒绝():
    store = LocalDirSpillStore(Path("."))  # root 不触达（空指针先拒）
    with pytest.raises(ValueError):
        await store.get("  ", tenant_id=str(uuid.uuid4()))


# ── K14-b：spill.get 兑换工具 ─────────────────────────────────────────────────


async def test_兑换端到端_落盘换指针_分窗续读至末窗(tmp_path: Path):
    # arrange：落盘 6000 字符原文（键含租户段），缺省窗 8000 一次取全；再验分窗与末窗
    store = LocalDirSpillStore(tmp_path)
    tenant = uuid.uuid4()
    text = "".join(f"行{i:04d}；" for i in range(750))  # 6000 字符
    locator = await store.put(f"spill/{tenant}/r/c.txt", text)
    binding = build_spill_retrieval_binding(store)
    ctx = _ctx(tenant)
    # act：全量兑换（缺省 offset/limit）
    whole = await binding.invoke(_call({"locator": locator}), ctx)
    # assert
    assert whole.ok is True
    assert whole.output["content"] == text and whole.output["total_chars"] == len(text)
    assert whole.output["offset"] == 0 and whole.output["limit"] == RETRIEVE_DEFAULT_LIMIT_CHARS
    assert whole.output["truncated"] is False
    # 分窗：两窗拼回原文，第二窗仍 truncated，末窗收口
    w1 = await binding.invoke(_call({"locator": locator, "limit": 100}), ctx)
    w2 = await binding.invoke(_call({"locator": locator, "offset": 100, "limit": 100}), ctx)
    assert w1.output["content"] == text[:100] and w1.output["truncated"] is True
    assert w2.output["content"] == text[100:200] and w2.output["offset"] == 100
    tail = await binding.invoke(_call({"locator": locator, "offset": len(text) - 50, "limit": 100}), ctx)
    assert tail.output["content"] == text[-50:] and tail.output["truncated"] is False


async def test_兑换读不到_结构化错误自带恢复指引(tmp_path: Path):
    # arrange：root 内同租户但文件不存在（已归档/清理形态）
    store = LocalDirSpillStore(tmp_path)
    tenant = uuid.uuid4()
    missing = str(tmp_path / f"spill/{tenant}/r/gone.txt")
    binding = build_spill_retrieval_binding(store)
    # act
    result = await binding.invoke(_call({"locator": missing}), _ctx(tenant))
    # assert：headroom 恢复指引范式——教模型「查复制完整性/原文可能已归档，重新执行取新指针」
    assert result.ok is False and result.error_code == int(ErrorCode.PARAM_INVALID)
    message = result.error_message or ""
    assert "恢复指引" in message and "归档" in message and "重新执行" in message


async def test_兑换拒绝跨租户与越界locator_错误带指引(tmp_path: Path):
    store = LocalDirSpillStore(tmp_path)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    locator = await store.put(f"spill/{tenant_a}/r/secret.txt", "他租户机密")
    binding = build_spill_retrieval_binding(store)
    # 跨租户：B 租户兑换 A 租户指针 → 结构化拒绝（防孤儿，store 侧收口）
    cross = await binding.invoke(_call({"locator": locator}), _ctx(tenant_b))
    assert cross.ok is False
    assert "跨租户" in (cross.error_message or "") and "恢复指引" in (cross.error_message or "")
    # 越界：root 外路径 → 结构化拒绝
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    escape = await binding.invoke(_call({"locator": str(outside)}), _ctx(tenant_a))
    assert escape.ok is False and "越界" in (escape.error_message or "")


async def test_兑换参数纵深防御_缺参_负offset_布尔limit全收口(tmp_path: Path):
    store = LocalDirSpillStore(tmp_path)
    tenant = uuid.uuid4()
    locator = await store.put(f"spill/{tenant}/r/c.txt", "内容")
    binding = build_spill_retrieval_binding(store)
    ctx = _ctx(tenant)
    missing_param = await binding.invoke(_call({}), ctx)
    assert missing_param.ok is False and missing_param.error_code == int(ErrorCode.PARAM_INVALID)
    negative = await binding.invoke(_call({"locator": locator, "offset": -1}), ctx)
    assert negative.ok is False and negative.error_code == int(ErrorCode.PARAM_INVALID)
    boolean = await binding.invoke(_call({"locator": locator, "limit": True}), ctx)  # bool 是 int 子类，显式排除
    assert boolean.ok is False and boolean.error_code == int(ErrorCode.PARAM_INVALID)


async def test_兑换工具_只读免审批_limit硬钳防回灌爆量(tmp_path: Path):
    # arrange：超硬钳的原文 + 超额 limit 请求
    store = LocalDirSpillStore(tmp_path)
    tenant = uuid.uuid4()
    text = "x" * (RETRIEVE_MAX_LIMIT_CHARS + 5_000)
    locator = await store.put(f"spill/{tenant}/r/big.txt", text)
    binding = build_spill_retrieval_binding(store)
    # act + assert：execution_mode=read（B1 基线放行，无审批面）
    assert binding.execution_mode is ExecutionMode.READ
    result = await binding.invoke(_call({"locator": locator, "limit": RETRIEVE_MAX_LIMIT_CHARS * 10}), _ctx(tenant))
    assert result.ok is True
    assert result.output["limit"] == RETRIEVE_MAX_LIMIT_CHARS  # 硬钳生效
    assert len(result.output["content"]) == RETRIEVE_MAX_LIMIT_CHARS
    assert result.output["truncated"] is True  # 尚有后文可续读


# ── K14-c：截断标记同源 + 组合根装配门 ────────────────────────────────────────


async def test_内核spill成功路径_预览内嵌同源locator标记():
    # arrange：超大结果 + 内存桩存储（put/get 双面，协议同形）
    class _FakeStore:
        def __init__(self) -> None:
            self.payloads: dict[str, str] = {}

        async def put(self, key: str, payload: str) -> str:
            self.payloads[key] = payload
            return f"mem://{key}"

        async def get(self, locator: str, *, tenant_id: str) -> str | None:
            return None

    big = {"text": "y" * (SPILL_THRESHOLD_CHARS + 100)}
    # act
    spilled = await spill_if_oversized(ToolResult(ok=True, output=big), _FakeStore(), key="spill/t/r/c.json")
    # assert：预览截断标注与 locator 同源同格式（K14-c；locator 字段并存供程序化读取）
    locator = spilled.output["locator"]
    assert locator == "mem://spill/t/r/c.json"
    assert SPILL_MARKER_FMT.format(locator=locator) in spilled.output["preview"]


def test_预览构造_有locator内嵌_无locator兜底同族():
    with_locator = build_preview_payload("a" * 99_999, locator="mem://k")
    assert SPILL_MARKER_FMT.format(locator="mem://k") in with_locator["preview"]
    without = build_preview_payload("a" * 99_999)
    assert "spilled" in without["preview"]  # 无 locator 兜底标注仍在同一标记格式族


def test_组合根spill装配门_未配置目录返回None():
    # spill 关闭（task_spill_dir=None）→ 条件装配分支不注册 spill.get 兑换工具
    assert _build_spill_store(Settings()) is None
