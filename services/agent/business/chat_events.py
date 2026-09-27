"""ChatStream 事件契约（计划 3.2：主干波 11 事件的生产侧单一事实源）。

权威设计：docs/architecture/02-网关层设计.md §5（SSE 主干波 11 事件表 + 帧格式）；
docs/Agent/Agent服务设计.md §6.3（AG-UI 16 事件语义蓝本的 M3 子集）。

本模块是**零依赖叶子模块**（仅 pydantic/enum，不 import 任何服务内模块）：
- chat_orchestrator（生产方）在此定义事件名与事件值对象；
- services/gateway/sse（编码方）跨 import 本模块取事件名常量做主干波校验——
  gateway→agent.business.叶子 不触任何契约禁区（叶子零传递链），换来 wire 名单源；
- 帧格式（id/event/data + 空行）与心跳语义归 L2 网关（02 §5），不在本模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ChatEventName(StrEnum):
    """主干波 11 事件（02 §5 波次列 M3 主干行，一个不多一个不少；wire 名=AG-UI 大写）。

    M4+ 扩展波（STATE_*/MESSAGES_SNAPSHOT/GATE_VERDICT）不在此登记——M3 网关不外发，
    前端对未知 event 名一律忽略（02 §5 事件分波裁决，协议向前兼容）。
    """

    RUN_STARTED = "RUN_STARTED"
    RETRIEVAL_EVIDENCE = "RETRIEVAL_EVIDENCE"
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
    TOOL_CALL_END = "TOOL_CALL_END"
    TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
    TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
    TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
    TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
    RUN_FINISHED = "RUN_FINISHED"
    RUN_ERROR = "RUN_ERROR"
    ROUTING_DECISION = "ROUTING_DECISION"  # 群聊路由决策系统事件（27 篇 X15；who/why 审计）


class ChatEvent(BaseModel):
    """对话事件值对象（frozen）：编排器产出 → 网关 hub 发布 + 帧编码的传输单元。

    data 载荷形状按 02 §5 事件表逐事件登记（见 chat_orchestrator 各发布点注释）；
    一律 JSON 可序列化标量/容器（standards/01 §2.4）。
    """

    model_config = ConfigDict(frozen=True)

    name: ChatEventName
    data: dict[str, Any] = Field(default_factory=dict)
    run_id: UUID | None = None  # 贯穿标识（审计对账用，不进 wire data）


class ChatCommand(BaseModel):
    """一次对话用例的输入命令（值对象 frozen）：端点受理事务提交后组装。"""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    user_id: UUID
    session_id: UUID
    task_id: UUID
    run_id: UUID
    agent_id: UUID | None = None
    message: str  # 本条用户消息（已落 PG messages）
    trace_id: str  # C2 贯穿（内核拒收空 trace_id）
    scopes: tuple[str, ...] = ("session:chat",)  # 主体授权面（B1 R3 唯一依据）
    adapter: str = "builtin"  # 适配器路由键（builtin | claude，Agent 服务设计 §3.2）
    member_system_prompt: str | None = None  # 群聊成员人格（27 篇；单 agent 会话 None）
    retrieval_top_k: int | None = None  # 覆盖 ChatPolicy.retrieval_top_k（缺省用策略值）


class ChatOutcome(BaseModel):
    """一次对话的终局结果（frozen）：回写与审计的载货（assistant 消息/引用/用量）。

    标识四元组齐全：结果汇（PG 短事务）按其定位聚合，无需闭包捕获请求级状态。
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    session_id: UUID
    task_id: UUID
    run_id: UUID
    status: str  # RunOutcome.status（completed/failed/timeout/...）
    answer: str = ""  # 助手回答全文（失败可为空）
    citations: list[dict[str, Any]] = Field(default_factory=list)  # RETRIEVAL_EVIDENCE.citations 同构
    usage: dict[str, Any] = Field(default_factory=dict)  # {token_in, token_out, cache_read_tokens}
    degraded: bool = False  # 检索/记忆降级标注（03 §3 步骤 0/3）
    error_code: int | None = None  # 02 §7 已登记码
    error_message: str | None = None
    retryable: bool = False  # 仅适配器错误/超时可重试（Agent 服务设计 §2 重试范围）
    cost_ms: int = 0
    agent_id: UUID | None = None  # 群聊发言归属（27 篇 X15；单 agent 会话=主 agent）


@dataclass(frozen=True)
class ChatPolicy:
    """对话编排策略（03 §1.1 ChatPolicy 的 M3 子集；默认值=建议初值，实测冻结）。

    归属叶子模块理由：chat_orchestrator 与 chat_context 双方消费，放任一方即参数环
    依赖；本模块零依赖，两侧同源。
    """

    total_budget_s: float = 60.0  # 对话总预算（内核时长维；耗尽 → RUN_ERROR 5001）
    tool_loop_max_rounds: int = 5  # 计划步数上限（03 §3「上限 5 轮防失控」）
    retrieval_top_k: int = 8  # 检索 top_k（对齐 api/01 §6.2）
    retrieval_retry_max: int = 1  # 检索自动重试（03 §3 步骤 3）
    rrf_k: int = 60  # L2 融合平滑常数（memory §3）
    half_life_days: float = 30.0  # L2 时间衰减半衰期（memory §3）
    # 在线忠实度抽检（architecture/10 §2 缺口②，落点 08 §7.4；2026-09-27 M5-2 追加）：
    # 完成路径按采样率抽中后记录 faithfulness 检查占位（LLM-as-judge 随评估批次接入）
    faithfulness_sampling_enabled: bool = True  # 开关默认开（10 篇 1% 口径）
    faithfulness_sample_rate: float = 0.01  # 采样率（run_id 确定性哈希桶；OA_ 环境变量覆盖）
