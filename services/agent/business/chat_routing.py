"""群聊发言路由解算器（27 篇 §5.2 四模式；确定性三模式 + orchestrator LLM 路由）。

权威设计：docs/架构设计/27-Agent群聊与工作流编排设计.md §2「发言编排四模式」
（mention/round_robin/all 三种确定性 + orchestrator 唯一 LLM 路由且审计 who/why）；
docs/Agent/03-多Agent资源与调度设计.md §4（多 agent 会话语义）。

推理分级宪法（第 2 条）的直接落法：
- 确定性三模式（mention/round_robin/all）为纯函数，零模型调用，同输入必同输出；
- orchestrator 是群聊内**唯一** LLM 路由模式：LLM 只做低频语义判断（选人+理由），
  产物（成员序号列表）必须过确定性校验（非法/越界/observer 序号一律丢弃，值不经采样），
  路由决策以 selected_by/reason 双字段落审计（宪法 5 全程可追溯：who/why）。

本模块为**零依赖叶子模块**（仅 pydantic/enum + 内嵌 Protocol，不 import 任何服务内模块）：
OrchestratorModel 形状与 services/platform/ports/model_port.py 的 ModelPort 一致，
去 import 直接内嵌 Protocol 防跨层耦合（同 chat_events 的叶子纪律）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RoutingMode(StrEnum):
    """发言编排四模式（27 篇 §2 RoutingModePicker；随会话记忆，wire 值=api/01 §5.2）。"""

    MENTION = "mention"
    ROUND_ROBIN = "round_robin"
    ALL = "all"
    ORCHESTRATOR = "orchestrator"


class MemberRef(BaseModel):
    """发言成员引用（frozen）：由调用方从 GroupMember 聚合投影。"""

    model_config = ConfigDict(frozen=True)

    agent_id: UUID
    display_name: str
    system_prompt: str = ""
    model: str | None = None  # 成员模型路由键（None=会话缺省 adapter）
    routing_role: str = "speaker"  # coordinator | speaker | observer（observer 永不发言）


class RoutingDecision(BaseModel):
    """路由决策（frozen）：selected_by=llm 时 reason 必填（27 篇：路由审计 who/why）。"""

    model_config = ConfigDict(frozen=True)

    mode: RoutingMode
    members: tuple[MemberRef, ...] = Field(default_factory=tuple)  # 本轮应答成员（按应答顺序）
    selected_by: str = "deterministic"  # deterministic | llm
    reason: str = ""  # llm 路由的 why（审计用）


class MemberRoster(Protocol):  # 成员花名册鸭子类型（调用方传 MemberRef 元组即可）
    ...


@runtime_checkable
class OrchestratorModel(Protocol):
    """协调者模型端口（内嵌 Protocol；形状 = model_port.ModelPort，防跨层耦合）。

    契约同 ModelPort：返回值已按 json_schema 校验可直接消费；任何失败抛异常，
    由本模块确定性兜底（路由永不因 LLM 故障阻断群聊）。
    """

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        timeout_s: float = 60.0,
        trace_id: str | None = None,
        num_ctx: int | None = None,
    ) -> dict[str, Any]:
        """执行一次结构化补全：system/user 提示 + JSON Schema 约束 → 合法 dict。"""
        ...  # pragma: no cover — Protocol 方法无实现


# 协调者提示与产物 schema（27 篇 §2 模式 4：选人 + 理由；selected=成员序号，1 起）。
_ORCHESTRATOR_SYSTEM = "你是群聊协调者，根据用户消息从成员列表中选择本轮应答者并给出理由。"
_ROUTING_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["selected", "reason"],
    "properties": {
        "selected": {"type": "array", "items": {"type": "integer"}},
        "reason": {"type": "string"},
    },
}


def _eligible_speakers(members: tuple[MemberRef, ...]) -> tuple[MemberRef, ...]:
    """可应答成员 = 非 observer（公共纪律：observer 任何模式不入选；花名册顺序保留）。"""
    return tuple(m for m in members if m.routing_role != "observer")


def _empty_decision(mode: RoutingMode, reason: str = "") -> RoutingDecision:
    """空决策（公共纪律：无可应答成员返回空决策，不抛异常）。"""
    return RoutingDecision(mode=mode, members=(), selected_by="deterministic", reason=reason)


def resolve_mention(message: str, members: tuple[MemberRef, ...]) -> RoutingDecision:
    """@点名路由（27 篇模式 1）：@display_name 即点名，未点名不消耗预算。

    - 提及语法精确匹配 display_name（大小写不敏感），observer 不可被点名；
    - ``@all`` 视为点名全部可应答成员（花名册顺序）；
    - 多成员点名按消息出现顺序排应答序；
    - 未命中任何 @提及（含只点了 observer）→ 回退第一个可应答成员（reason 注明）；
    - 无可应答成员 → 空 members 决策（不抛异常）。
    """
    speakers = _eligible_speakers(members)
    if not speakers:
        return _empty_decision(RoutingMode.MENTION)
    lowered = message.lower()
    if "@all" in lowered:
        names = "、".join(m.display_name for m in speakers)
        return RoutingDecision(mode=RoutingMode.MENTION, members=speakers, reason=f"@all 点名全员应答：{names}")
    hits: list[tuple[int, MemberRef]] = []
    for member in speakers:
        pos = lowered.find(f"@{member.display_name.lower()}")
        if pos >= 0:
            hits.append((pos, member))
    if not hits:
        first = speakers[0]
        return RoutingDecision(
            mode=RoutingMode.MENTION,
            members=(first,),
            reason=f"无显式点名，回默认发言者：{first.display_name}",
        )
    hits.sort(key=lambda hit: hit[0])  # 按出现位置排应答序
    selected = tuple(member for _, member in hits)
    names = "、".join(m.display_name for m in selected)
    return RoutingDecision(mode=RoutingMode.MENTION, members=selected, reason=f"显式点名：{names}")


def resolve_round_robin(message: str, members: tuple[MemberRef, ...], *, assistant_count: int) -> RoutingDecision:
    """轮询路由（27 篇模式 2）：按成员序依次应答，只选游标位一个成员。

    轮询游标 = assistant_count % len(speakers)——由既有历史 assistant 消息数派生
    （调用方从会话窗口计数传入），**零新增持久状态**：无游标存储、无并发争用、
    重放可复现（本裁决，2026-09-28 定稿）。
    """
    speakers = _eligible_speakers(members)
    if not speakers:
        return _empty_decision(RoutingMode.ROUND_ROBIN)
    picked = speakers[assistant_count % len(speakers)]
    return RoutingDecision(
        mode=RoutingMode.ROUND_ROBIN,
        members=(picked,),
        reason=f"轮询第 {assistant_count} 轮命中：{picked.display_name}",
    )


def resolve_all(message: str, members: tuple[MemberRef, ...]) -> RoutingDecision:
    """多答对比路由（27 篇模式 3）：全部可应答成员按花名册顺序并行应答。

    答案差异进 ResponseGroup 并列渲染 + 用户选优；空 speakers → 空决策（不抛异常）。
    """
    speakers = _eligible_speakers(members)
    names = "、".join(m.display_name for m in speakers)
    return RoutingDecision(mode=RoutingMode.ALL, members=speakers, reason=f"多答对比全员应答：{names}")


async def resolve_orchestrator(
    message: str, members: tuple[MemberRef, ...], model: OrchestratorModel, trace_id: str
) -> RoutingDecision:
    """协调者路由（27 篇模式 4）：群聊唯一 LLM 路由模式，路由决定写审计 who/why。

    - LLM 产物 = 1 起始成员序号列表 + 理由（json_schema 约束）；非法/越界/observer
      序号确定性丢弃（推理分级宪法 2：LLM 输出必须过确定性校验，值不经采样）；
    - reason 必以成员 display_name 拼写 who（如「由Alice应答：…」），selected_by="llm"；
    - 有效选择为空 / LLM 调用失败 → 确定性回退第一个可应答成员（reason 注明回退，
      不抛异常——路由永不因 LLM 故障阻断群聊）。
    """
    if not members:
        return _empty_decision(RoutingMode.ORCHESTRATOR)
    roster_lines = "\n".join(f"{i}. {m.display_name}（{m.routing_role}）" for i, m in enumerate(members, start=1))
    user = f"成员列表：\n{roster_lines}\n\n用户消息：{message}"
    selected: list[MemberRef] = []
    llm_reason = ""
    llm_failed = False
    try:
        payload = await model.complete_structured(
            system=_ORCHESTRATOR_SYSTEM,
            user=user,
            json_schema=_ROUTING_JSON_SCHEMA,
            trace_id=trace_id,
        )
        if isinstance(payload, dict):
            raw_reason = payload.get("reason", "")
            llm_reason = raw_reason if isinstance(raw_reason, str) else ""
            raw_selected = payload.get("selected", [])
            seen: set[int] = set()
            if isinstance(raw_selected, list):
                for idx in raw_selected:
                    # 确定性校验：只收合法 1 起始序号且非 observer；bool 冒充 int 一并丢弃
                    if not isinstance(idx, int) or isinstance(idx, bool) or idx in seen:
                        continue
                    if 1 <= idx <= len(members):
                        member = members[idx - 1]
                        if member.routing_role != "observer":
                            seen.add(idx)
                            selected.append(member)
    except Exception:  # noqa: BLE001 — LLM 任何失败都走确定性回退（standards/01 §2.6 兜底）
        llm_failed = True
        selected = []
    if selected:
        names = "、".join(m.display_name for m in selected)
        reason = f"由{names}应答：{llm_reason}" if llm_reason else f"由{names}应答"
        return RoutingDecision(mode=RoutingMode.ORCHESTRATOR, members=tuple(selected), selected_by="llm", reason=reason)
    speakers = _eligible_speakers(members)
    if not speakers:
        return _empty_decision(RoutingMode.ORCHESTRATOR, reason="协调者无可应答成员")
    first = speakers[0]
    fallback_why = "LLM 路由失败，回默认发言者" if llm_failed else "协调者未给出有效选择，回默认发言者"
    return RoutingDecision(
        mode=RoutingMode.ORCHESTRATOR,
        members=(first,),
        reason=f"{fallback_why}：{first.display_name}",
    )
