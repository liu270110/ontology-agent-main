# tests/agent/test_cap_todo.py
"""todo 能力测试（docs/Agent/06 路线 #5）：受控申报面 + 内核判据联动正反例。

核心裁决测试面（判据权威=02 §2 A4「不认模型自述完成」+ T2「插件只能申报不能裁决」；
争议裁决=研究整理 08 §6 C5）：
- 申报 done 判据满足 → 权威 done（判定在判据侧，非模型自述）；
- 申报 done 判据未满足 → 候选 + 差距说明，权威状态不变；
- 无判据引用项 done → 仅候选（人工/规划侧裁决）；
- 回执后到 → todo_read 合成权威 done、候选关闭（完成判定永在判据侧）；
- 枚举外/超限/超长/未知判据引用/未知项 id → 结构化拒绝。
判据联动用真内核件（CriterionEvaluator + 内存 KernelLedger），零 mock、零真连；
AAA + 中文命名。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from services.agent.business.capabilities.todo import (
    TODO_ACTION_IRIS,
    TODO_MAX_CONTENT_CHARS,
    TODO_MAX_ITEMS,
    CriteriaLink,
    DeclarationEnvelope,
    TodoItemStatus,
    TodoList,
    TodoToolError,
    build_todo_bindings,
    todo_read,
    todo_update,
    todo_write,
)
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.domain.model.kernel_actions import ExecutionMode, ToolCall
from services.agent.domain.model.kernel_context import TenantContext
from services.agent.domain.model.kernel_planning import SuccessCriterion
from services.platform.errors import ErrorCode

_BINDING_LOGGER = "services.agent.business.capabilities.todo.bindings"


# ── 夹具与构造器 ───────────────────────────────────────────────────────────────


@pytest.fixture()
def criteria() -> tuple[SuccessCriterion, ...]:
    """计划判据集：交付回执 + 人工复核回执两类（focus 同一任务节点）。"""
    return (
        SuccessCriterion(
            criterion_id="crit-delivery",
            focus_iri="urn:task:power-outage",
            required_receipt_kind="delivery_confirmation",
            description="停电分析交付回执",
        ),
        SuccessCriterion(
            criterion_id="crit-review",
            focus_iri="urn:task:power-outage",
            required_receipt_kind="review_signoff",
            description="人工复核回执",
        ),
    )


@pytest.fixture()
def ledger() -> KernelLedger:
    """单运行内存账本（真内核件，零外呼）。"""
    return KernelLedger(tenant_id=uuid.uuid4(), trace_id="trace-todo-cap-test")


@pytest.fixture()
def link(criteria: tuple[SuccessCriterion, ...], ledger: KernelLedger) -> CriteriaLink:
    """判据联动面：真 CriterionEvaluator + 内存账本。"""
    return CriteriaLink(criteria=criteria, ledger=ledger)


def _envelope(by: str = "model-test") -> DeclarationEnvelope:
    return DeclarationEnvelope(declared_by=by, declared_at=datetime.now(tz=UTC), basis="trace=trace-todo-cap-test")


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), trace_id="trace-todo-cap-test")


def _call(tool: str, parameters: dict[str, Any]) -> ToolCall:
    mode = ExecutionMode.WRITE if tool in {"write", "update"} else ExecutionMode.READ
    return ToolCall(
        action_iri=TODO_ACTION_IRIS[tool],
        execution_mode=mode,
        parameters=parameters,
        param_hash="hash-todo-test",
    )


# ── todo_write / todo_update：申报态与权威镜像 ─────────────────────────────────


def test_整表申报_进行中申报照记_权威镜像无需判据(link: CriteriaLink):
    # Arrange：两项非完成申报（pending/in_progress）
    items = [
        {"content": "拉取馈线台账", "status": "in_progress"},
        {"content": "撰写停电分析报告", "status": "pending"},
    ]
    # Act
    view = todo_read(todo_write(items=items, envelope=_envelope(), link=link), link)
    # Assert：申报态=权威态（无完成主张，无需判据），无候选
    assert view.total == 2
    assert [item.authority_status for item in view.items] == [
        TodoItemStatus.IN_PROGRESS,
        TodoItemStatus.PENDING,
    ]
    assert [item.declared_status for item in view.items] == [
        TodoItemStatus.IN_PROGRESS,
        TodoItemStatus.PENDING,
    ]
    assert all(not item.candidate_open for item in view.items)
    assert [item.item_id for item in view.items] == ["todo-01", "todo-02"]  # 自动 id 确定性分配


def test_update_单项申报_申报态与权威镜像同步(link: CriteriaLink):
    # Arrange：整表申报两项 pending
    table = todo_write(items=[{"content": "任务甲"}, {"content": "任务乙"}], envelope=_envelope(), link=link)
    # Act：单项申报任务甲 in_progress
    updated = todo_update(table, item_id="todo-01", status="in_progress", envelope=_envelope("model-2"))
    view = todo_read(updated, link)
    # Assert：仅目标项变化，且申报信封换新（可追溯）
    assert view.items[0].declared_status is TodoItemStatus.IN_PROGRESS
    assert view.items[0].authority_status is TodoItemStatus.IN_PROGRESS
    assert view.items[1].declared_status is TodoItemStatus.PENDING
    assert view.items[0].declared_by == "model-2"


# ── 判据联动：完成判定永在内核判据侧（A4/T2 核心裁决）─────────────────────────


def test_申报done_判据回执已落账_权威态转done(
    criteria: tuple[SuccessCriterion, ...], ledger: KernelLedger, link: CriteriaLink
):
    # Arrange：判据所需外部回执已落账（externally_verified 凭证源）
    ledger.record_external_receipt(
        kind="delivery_confirmation", focus_iri="urn:task:power-outage", payload={"receipt": "R-1"}
    )
    items = [{"content": "交付停电分析", "status": "done", "criterion_refs": ["crit-delivery"]}]
    # Act
    view = todo_read(todo_write(items=items, envelope=_envelope(), link=link), link)
    # Assert：权威 done 来自判据侧求值（账本回执），非模型自述
    assert view.items[0].authority_status is TodoItemStatus.DONE
    assert view.items[0].candidate_open is False
    assert view.authority_done == 1 and view.candidate_open == 0


def test_申报done_判据未满足_记候选附差距说明_权威状态不变(
    criteria: tuple[SuccessCriterion, ...], ledger: KernelLedger, link: CriteriaLink
):
    # Arrange：先 in_progress 申报建立工作态基准（账本无回执）
    table = todo_write(
        items=[
            {
                "content": "交付停电分析",
                "status": "in_progress",
                "item_id": "deliver",
                "criterion_refs": ["crit-delivery"],
            }
        ],
        envelope=_envelope(),
        link=link,
    )
    # Act：申报 done（判据未满足）
    declared = todo_update(table, item_id="deliver", status="done", envelope=_envelope())
    view = todo_read(declared, link)
    # Assert：申报记候选 + 差距说明点名所需回执；权威状态不变（维持 in_progress）
    row = view.items[0]
    assert row.declared_status is TodoItemStatus.DONE
    assert row.authority_status is TodoItemStatus.IN_PROGRESS  # 不改变权威状态
    assert row.candidate_open is True
    assert "delivery_confirmation" in row.gap_note and "缺少" in row.gap_note  # 差距说明可执行


def test_无判据引用项申报done_仅记候选待人工裁决(link: CriteriaLink):
    # Arrange：项不引用任何判据
    items = [{"content": "整理会议纪要", "status": "done"}]
    # Act
    view = todo_read(todo_write(items=items, envelope=_envelope(), link=link), link)
    # Assert：仅记候选（人工/规划侧裁决），权威状态不变
    row = view.items[0]
    assert row.candidate_open is True
    assert row.authority_status is TodoItemStatus.PENDING
    assert "无判据引用" in row.gap_note and "人工/规划侧" in row.gap_note


def test_回执后到_todo_read合成权威done_候选关闭(
    criteria: tuple[SuccessCriterion, ...], ledger: KernelLedger, link: CriteriaLink
):
    # Arrange：申报 done 时回执未到→候选
    table = todo_write(
        items=[{"content": "交付停电分析", "status": "done", "criterion_refs": ["crit-delivery"]}],
        envelope=_envelope(),
        link=link,
    )
    before = todo_read(table, link)
    assert before.items[0].candidate_open is True  # 前置：候选未决
    # Act：外部回执后到，重新合成权威视图
    ledger.record_external_receipt(
        kind="delivery_confirmation", focus_iri="urn:task:power-outage", payload={"receipt": "R-2"}
    )
    after = todo_read(table, link)
    # Assert：权威态由判据侧翻转 done，候选关闭——申报未变、判定在判据侧
    assert after.items[0].authority_status is TodoItemStatus.DONE
    assert after.items[0].candidate_open is False
    assert after.authority_done == 1 and after.candidate_open == 0


def test_多判据引用_部分满足仍记候选_权威状态不变(
    criteria: tuple[SuccessCriterion, ...], ledger: KernelLedger, link: CriteriaLink
):
    # Arrange：项引用两条判据，仅一条回执落账
    ledger.record_external_receipt(
        kind="delivery_confirmation", focus_iri="urn:task:power-outage", payload={"receipt": "R-3"}
    )
    items = [
        {"content": "交付并过审", "status": "done", "criterion_refs": ["crit-delivery", "crit-review"]},
    ]
    # Act
    view = todo_read(todo_write(items=items, envelope=_envelope(), link=link), link)
    # Assert：全部判据满足才生效；缺复核回执→候选，差距说明点名缺项
    row = view.items[0]
    assert row.authority_status is TodoItemStatus.PENDING
    assert row.candidate_open is True
    assert "review_signoff" in row.gap_note


# ── 护栏：枚举白名单 / 规模 / 内容 / 引用与 id 一致性（deny-by-default）────────


def test_状态枚举外申报_结构化拒绝(link: CriteriaLink):
    # Arrange
    items = [{"content": "任务甲", "status": "completed"}]  # 枚举外
    # Act + Assert
    with pytest.raises(TodoToolError) as exc_info:
        todo_write(items=items, envelope=_envelope(), link=link)
    assert exc_info.value.code is ErrorCode.PARAM_INVALID
    assert "pending/in_progress/done" in exc_info.value.message  # 消息点名合法三态（修正动作）


def test_项数超过上限_结构化拒绝(link: CriteriaLink):
    # Arrange：51 项超 50 上限
    items = [{"content": f"任务{i}"} for i in range(TODO_MAX_ITEMS + 1)]
    # Act + Assert
    with pytest.raises(TodoToolError, match="上限"):
        todo_write(items=items, envelope=_envelope(), link=link)


def test_单项内容超过长度上限_结构化拒绝(link: CriteriaLink):
    # Arrange
    items = [{"content": "长" * (TODO_MAX_CONTENT_CHARS + 1)}]
    # Act + Assert
    with pytest.raises(TodoToolError, match="单项上限"):
        todo_write(items=items, envelope=_envelope(), link=link)


def test_空白内容_结构化拒绝(link: CriteriaLink):
    # Arrange
    # Act + Assert
    with pytest.raises(TodoToolError, match="空白"):
        todo_write(items=[{"content": "   "}], envelope=_envelope(), link=link)


def test_判据引用不在计划判据集_结构化拒绝(link: CriteriaLink):
    # Arrange
    items = [{"content": "任务甲", "status": "done", "criterion_refs": ["crit-ghost"]}]
    # Act + Assert
    with pytest.raises(TodoToolError) as exc_info:
        todo_write(items=items, envelope=_envelope(), link=link)
    assert "crit-ghost" in exc_info.value.message
    assert "crit-delivery" in exc_info.value.message  # 消息列出可用判据（修正动作）


def test_显式item_id重复_结构化拒绝(link: CriteriaLink):
    # Arrange
    items = [{"content": "任务甲", "item_id": "dup"}, {"content": "任务乙", "item_id": "dup"}]
    # Act + Assert
    with pytest.raises(TodoToolError, match="重复"):
        todo_write(items=items, envelope=_envelope(), link=link)


def test_update_未知项id_结构化拒绝并给修正指引(link: CriteriaLink):
    # Arrange：表内已有 todo-01
    table = todo_write(items=[{"content": "任务甲"}], envelope=_envelope(), link=link)
    # Act + Assert
    with pytest.raises(TodoToolError) as exc_info:
        todo_update(table, item_id="todo-99", status="done", envelope=_envelope())
    assert exc_info.value.code is ErrorCode.PARAM_INVALID
    assert "todo-01" in exc_info.value.message  # 现有 id 进消息（修正动作）


def test_空整表申报_结构化拒绝(link: CriteriaLink):
    # Arrange
    # Act + Assert
    with pytest.raises(TodoToolError, match="至少申报一项"):
        todo_write(items=[], envelope=_envelope(), link=link)


# ── ToolPort 绑定形状（tools.bindings 通道，B1 门禁参数域收口依赖 schema）───────


def test_三绑定经分发器注册_行动类可查且读写分级正确(criteria: tuple[SuccessCriterion, ...], ledger: KernelLedger):
    # Arrange：分发器注册面自带 meta/semver/action_iri 校验（B1 R1 注册前置）
    dispatcher = ExtensionDispatcher()
    write_tool, update_tool, read_tool = build_todo_bindings(criteria=criteria, ledger=ledger)
    # Act
    for tool in (write_tool, update_tool, read_tool):
        dispatcher.register_tool(tool)
    # Assert
    for action_iri in TODO_ACTION_IRIS.values():
        assert dispatcher.tool_for(action_iri) is not None
    assert write_tool.meta.name == "todo.write" and update_tool.meta.name == "todo.update"
    assert write_tool.execution_mode is ExecutionMode.WRITE  # 写类判级（B5 审批路由依据）
    assert read_tool.execution_mode is ExecutionMode.READ  # 只读基线放行
    assert write_tool.input_schema["additionalProperties"] is False  # schema 收敛（禁未知键）


async def test_绑定invoke_枚举外状态_结构化失败不裸异常(criteria: tuple[SuccessCriterion, ...], ledger: KernelLedger):
    # Arrange
    write_tool, _, _ = build_todo_bindings(criteria=criteria, ledger=ledger)
    # Act
    result = await write_tool.invoke(_call("write", {"items": [{"content": "任务甲", "status": "finished"}]}), _ctx())
    # Assert：结构化 ToolResult（ok=False + 登记错误码），禁裸异常逃逸
    assert result.ok is False
    assert result.error_code == int(ErrorCode.PARAM_INVALID)
    assert "pending/in_progress/done" in (result.error_message or "")


async def test_绑定invoke_申报与读取闭环_视图计数随判据翻转(
    criteria: tuple[SuccessCriterion, ...], ledger: KernelLedger
):
    # Arrange：三绑定共享一块看板
    write_tool, update_tool, read_tool = build_todo_bindings(criteria=criteria, ledger=ledger)
    # Act：整表申报（含 done 申报，判据未满足）→ 读视图 → 回执落账 → 再读
    written = await write_tool.invoke(
        _call(
            "write",
            {
                "items": [
                    {"content": "交付停电分析", "status": "done", "criterion_refs": ["crit-delivery"]},
                    {"content": "归档底稿", "status": "pending"},
                ]
            },
        ),
        _ctx(),
    )
    before = await read_tool.invoke(_call("read", {}), _ctx())
    ledger.record_external_receipt(
        kind="delivery_confirmation", focus_iri="urn:task:power-outage", payload={"receipt": "R-4"}
    )
    after = await read_tool.invoke(_call("read", {}), _ctx())
    updated = await update_tool.invoke(_call("update", {"item_id": "todo-02", "status": "in_progress"}), _ctx())
    # Assert：申报面闭环 + 判据侧翻转 + update 生效
    assert written.ok and written.output["total"] == 2
    assert before.output["items"][0]["candidate_open"] is True  # 回执未到：候选
    assert after.output["items"][0]["authority_status"] == "done"  # 回执后到：权威 done
    assert updated.ok and updated.output["items"][1]["authority_status"] == "in_progress"


async def test_写类申报落结构化审计日志_含摘要与trace(
    criteria: tuple[SuccessCriterion, ...], ledger: KernelLedger, caplog: pytest.LogCaptureFixture
):
    # Arrange
    write_tool, _, _ = build_todo_bindings(criteria=criteria, ledger=ledger)
    # Act
    with caplog.at_level(logging.INFO, logger=_BINDING_LOGGER):
        result = await write_tool.invoke(_call("write", {"items": [{"content": "任务甲"}]}), _ctx())
    # Assert
    assert result.ok is True
    audit_records = [r for r in caplog.records if "todo.audit" in r.getMessage()]
    assert len(audit_records) == 1
    message = audit_records[0].getMessage()
    assert "todo.write" in message and "trace_id=trace-todo-cap-test" in message  # 工具名 + trace 透传
    assert '"total": 1' in message  # 含申报结果摘要（计数）


def test_空看板读取_返回零项视图(link: CriteriaLink):
    # Arrange：空看板（尚未申报）
    empty = TodoList()
    # Act
    view = todo_read(empty, link)
    # Assert
    assert view.total == 0
    assert view.items == ()
    assert view.authority_done == 0 and view.candidate_open == 0
