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
    """SSE 事件名单一事实源（wire 名=AG-UI 大写）。

    主干波 11 事件（02 §5 波次列 M3 主干行）+ 群聊路由决策（27 篇 X15）+
    执行结构波六事件（40 篇 §4.1「执行结构波」，2026-10-04 R2 登记；契约权威=api/02 §3）。
    其余 M4+ 扩展波（STATE_*/MESSAGES_SNAPSHOT/GATE_VERDICT）不在此登记——
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
    # 思考流三事件（02 协议 THINKING_* 注记，reasoning 透传批 2026-10-07；挂 TEXT_MESSAGE_*
    # 同族消息路径）：START {message_id, reasoning_effort?} / CONTENT {message_id, delta} /
    # END {message_id}——每消息至多一对 START/END。落库口径：CONTENT 纯实时不落 task_events
    # （EventSink 双写豁免，SUBRUN_UPDATED 先例）；START/END 落账本（exec_events.py 集合）。
    THINKING_START = "THINKING_START"
    THINKING_CONTENT = "THINKING_CONTENT"
    THINKING_END = "THINKING_END"
    TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
    RUN_FINISHED = "RUN_FINISHED"
    RUN_ERROR = "RUN_ERROR"
    ROUTING_DECISION = "ROUTING_DECISION"  # 群聊路由决策系统事件（27 篇 X15；who/why 审计）
    # M4.5-A 运行中输入面回执（docs/Agent/12-M4.5运行中输入面与模型韧性设计（主仓本地）§1.4；
    # 用户可见回执：inbox 提交受理即发布，消费面后续批；前端对未知事件名静默忽略已核实）
    INBOX_SPLICED = "INBOX_SPLICED"
    # ── 执行结构波（40 篇 §4.1；payload schema=40 篇 §4.2，转译/落库纪律=exec_events.py）──
    PLAN_UPDATED = "PLAN_UPDATED"  # 计划整表快照（last-wins；发射点=规划 R4）
    SUBRUN_STARTED = "SUBRUN_STARTED"  # 子 run 生命周期（发射点=spawn，40 篇 R2）
    SUBRUN_UPDATED = "SUBRUN_UPDATED"  # 子 run 心跳（纯实时不落库；R5 可缓发）
    SUBRUN_FINISHED = "SUBRUN_FINISHED"  # 子 run 终态（含 rejected_artifact，40 篇 §3.2）
    WORKFLOW_NODE_STARTED = "WORKFLOW_NODE_STARTED"  # 工作流节点开始（X16 才有发射点，R7）
    WORKFLOW_NODE_FINISHED = "WORKFLOW_NODE_FINISHED"  # 工作流节点终态（X16 才有发射点，R7）
    # ── 审批波双事件（02 协议行 67/68，2026-10-05 五组登记；设计源=08 篇事件 14+11 篇状态机）──
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"  # 审批挂起（发射点=kernel.approval_pending 转译上 wire，W2-2）
    # 审批裁决落定（发射点=approval_service.decide 成功路径：SSE 实时 +
    # outbox approval.resolved 双通道，恒先于 resume 生效）。
    APPROVAL_RESOLVED = "APPROVAL_RESOLVED"


class ChatEvent(BaseModel):
    """对话事件值对象（frozen）：编排器产出 → 网关 hub 发布 + 帧编码的传输单元。

    data 载荷形状按 02 §5 事件表逐事件登记（见 chat_orchestrator 各发布点注释）；
    一律 JSON 可序列化标量/容器（standards/01 §2.4）。
    trace_id（40 篇 §4.2 公共字段，R2 增补、可选向后兼容）：经 :func:`wire_data`
    只补缺合并进 wire payload 与 task_events.data（宪法 5 可追溯 + 40 篇 §7.3 trace 瀑布）。
    """

    model_config = ConfigDict(frozen=True)

    name: ChatEventName
    data: dict[str, Any] = Field(default_factory=dict)
    run_id: UUID | None = None  # 贯穿标识（审计对账用，不进 wire data）
    trace_id: str | None = None  # C2 贯穿（40 篇 §4.2；None=不进 wire data，主干事件行为不变）


def wire_data(event: ChatEvent) -> dict[str, Any]:
    """事件外发/落库统一载荷（40 篇 §4.2）：data 浅拷贝 + trace_id 只补缺（不覆盖）。

    SSE 发布（sessions._chat_stream_response）与 task_events 双写（双写钩子/task_worker
    _drain_orchestrator）同源取本函数——wire payload 与回放行 data 保持一致；未设
    trace_id 的既有主干事件行为不变（向后兼容）。
    """
    data = dict(event.data)
    if event.trace_id and "trace_id" not in data:
        data["trace_id"] = event.trace_id
    return data


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
    # C4 trace 贯通（红队审查 §5 修复批 2026-10-07）：受理时记录的网关原始 trace（task.payload
    # origin_trace_id 回溯）——worker 重放时优先复用为命令 trace（保持与受理同链），事件 payload
    # 另注 original_trace_id 便于按网关 trace 聚合检索；None=无法回溯（worker 合成 trace 兜底）。
    original_trace_id: str | None = None
    # C2 EXTERNAL_WRITE 幂等锚（红队审查 §5 修复批 2026-10-07）：attempt 维度幂等键
    # （key=task_id:attempt，worker 重试监督链注入）——经内核注入写动作工具调用参数与审批
    # 工单（param_hash 绑定），工具实现侧幂等后续批接键（本批保键贯通可见+审计落账）。
    idempotency_key: str | None = None
    task_type: str = "chat"  # task.type 透传（40 篇 §4.2：RUN_STARTED.task_type；chat|workflow_run|…，缺省 chat）
    scopes: tuple[str, ...] = ("session:chat",)  # 主体授权面（B1 R3 唯一依据）
    adapter: str = "builtin"  # 适配器路由键（builtin | claude，Agent 服务设计 §3.2）
    member_system_prompt: str | None = None  # 群聊成员人格（27 篇；单 agent 会话 None）
    retrieval_top_k: int | None = None  # 覆盖 ChatPolicy.retrieval_top_k（缺省用策略值）
    approvals: tuple[Any, ...] = ()  # 运行中审批票（H-0b：worker 携票重放并入内核 approvals；预授权/审批回执两源）
    # M4.5-A P-4 resume 计划对账锚点（docs/Agent/12 §1.3；元素形状 {seq, action_iri,
    # param_hash}）：worker 重试/续跑重放时携前序 Run 的 kernel.step_validated 锚点，
    # 内核规划完成后对账——全等匹配且 execution_mode=READ 才特批跳过。
    resumed_validated: tuple[Any, ...] = ()


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
    # 注入防御链（docs/Agent/15 §2 F2）：上下文威胁扫描剥离开关——组合根缺省读统一配置层
    # （Settings.context_threat_scan_enabled，OA_ 环境变量覆盖）；显式传入 policy 时以
    # policy 值为准（faithfulness 同款纪律）。False=不扫描不剥离零事件（零行为变化）。
    context_threat_scan_enabled: bool = True
