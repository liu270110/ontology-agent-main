"""平台 HTTP 内核：统一错误码/统一错误体/GatewayError/trace 上下文（02 §3/§7）。

2026-09-27 模块轴重构：错误契约是 gateway 中间件与各模块 api 的共享面，归 platform 底座；
HTTP 中间件链本体仍在 services.gateway.middlewares（re-export 兼容旧 import）。
"""

from __future__ import annotations

from contextvars import ContextVar
from enum import IntEnum

from starlette.requests import Request
from starlette.responses import JSONResponse

# ---------------------------------------------------------------- 统一错误码（02 §7 全表登记）


class ErrorCode(IntEnum):
    """02 §7 / api/01 §4.3 已登记错误码（新增必须先回 02 §7 登记）。"""

    TOKEN_MISSING = 1001
    TOKEN_INVALID = 1002
    TOKEN_EXPIRED = 1003
    # 2026-10-04 be1 批登记（docs/评审/联调缺陷台账-2026-10-04（主仓本地）B-⑥：FastAPI 默认
    # 404/405 错误体 {detail} 不合统一四字段契约——网关全局 http_exception_handler 转四字段时
    # 既有族无「路由不存在/方法不允许」专用码，就近 1xxx 网关段新增；02 §7 表格回填随文档批）
    ROUTE_NOT_FOUND = 1004
    METHOD_NOT_ALLOWED = 1005
    SCOPE_INSUFFICIENT = 2001
    ROLE_FORBIDDEN = 2002
    TENANT_MISMATCH = 2003
    TENANT_DISABLED = 2004
    RATE_LIMITED = 2005
    PARAM_INVALID = 3001
    BODY_MALFORMED = 3002
    VERSION_CONFLICT = 3003
    UNSUPPORTED_MEDIA_TYPE = 3004
    SESSION_CLOSED = 4101
    TASK_ALREADY_RUNNING = 4102
    TOOL_BUSY = 4103  # 2026-09-26 缺口核查修复补登记（41xx session 段）
    # 2026-10-04 M4.5-A 批登记（docs/Agent/12-M4.5运行中输入面与模型韧性设计（主仓本地）§1.2/
    # §1.4；02 §7 表格回填随文档批）：4104=紧急停止激活拒新工作（worker submit 前与内核步
    # 边界闸门共用）；4105=目标 Run 不在本进程运行注册表（inbox 端点归属/可达性口径）。
    ESTOP_ACTIVE = 4104
    RUN_NOT_LOCAL = 4105
    # 2026-10-05 M4.6-D2 批登记（docs/Agent/13 §2.3；02 §7 表格回填随文档批）：
    # 4106=rewind 锚点非法（before_seq 不存在或非用户轮——仅用户消息 seq 可作回退锚）。
    SESSION_REWIND_INVALID = 4106
    # 2026-10-07 工作区面板批登记（docs/架构设计/31-Agent工作区面板与终端后端API需求；
    # 02 §7 表格回填随文档批）：4001=受限终端只读白名单拒绝（白名单外命令/参数绝对路径
    # 与 .. 穿越/find 写执行参数/Windows .bat·.cmd·.ps1 可执行后缀）。
    TERMINAL_CMD_NOT_ALLOWED = 4001
    # 42xx 段（与 4201 VERSION_IMMUTABLE 同段）：inbox 每 Run 容量上限拒绝（§1.1）
    INBOX_CAPACITY = 4203
    VERSION_IMMUTABLE = 4201
    SSE_REPLAY_EXPIRED = 4301
    OBJECT_ALREADY_IN_REVIEW = 4701
    LLM_TIMEOUT = 5001
    LLM_UNAVAILABLE = 5002
    MCP_TARGET_UNAVAILABLE = 5003
    STORAGE_UNAVAILABLE = 5004
    RETRY_BUDGET_EXHAUSTED = 5005  # 2026-09-26 缺口核查修复补登记（5xxx 段）
    # 2026-09-29 H-0c 批登记（孤儿 Run 回收：running 悬挂超时→对账回收；02 §7 表格回填随文档批）
    ORPHAN_RUN_RECOVERED = 5006
    # 2026-10-03 B-② 批登记（中断账本合成闭合：崩溃恢复对撕裂投影合成 close/步终态的中断语义码；
    # 既有族无「工具被中断」码——4101 是用户取消清单口径，5003 是工具超时口径，均不复用；
    # 依据 docs/Agent/10 §4.1 ②，02 §7 表格回填随文档批）
    TOOL_CALL_INTERRUPTED = 5007
    # 2026-10-05 K1 批登记（内核循环检测两段式：同签名连续重复达阈值硬终止，A-1 上游
    # gemini-cli；依据 docs/Agent/13 §2 K1-a；02 §7 表格回填随文档批）
    EXEC_LOOP_DETECTED = 5008
    # 2026-10-05 S1 批登记（docs/Agent/14-集市平台核心模块与ORSI原子能力设计（主仓本地）§2/§3：
    # 工具集市 46xx 段三码；02 §7 表格回填随文档批）——4601=注册清单校验拒绝（缺语义标注等，
    # 可修复）；4602=同租户同名工具已登记；4603=生命周期非法状态迁移（硬编码迁移表拒绝）。
    TOOL_LISTING_INVALID = 4601
    TOOL_NAME_TAKEN = 4602
    TOOL_ILLEGAL_TRANSITION = 4603
    # 2026-10-07 F1 批登记（docs/Agent/15-下一波后端推进设计（主仓本地）§1.1：workflows 域
    # 48xx 段三码；02 §7 表格回填随文档批）——4801=工作流图结构违规（边端点不存在/start
    # 不唯一/end 不可达/DAG 有环，detail 结构化列违规项）；4802=节点校验违规（八类节点
    # kind 非法或必填缺失，如 condition 节点必带确定性表达式、禁裸 LLM 语义分支）；
    # 4803=非草稿改动（仅草稿可 PUT/DELETE，已发布版本不可变——27 篇 §3 候选非成品落法）。
    WORKFLOW_GRAPH_INVALID = 4801
    WORKFLOW_NODE_INVALID = 4802
    WORKFLOW_NOT_DRAFT = 4803
    # 2026-10-07 X16 执行引擎批登记（api/01 §5.11 POST /workflows/{id}/runs 409* 口径）：
    # 4804=工作流未发布（正式运行仅 published；试运行走 /test 跑草稿——27 篇 §3「草稿可随意改
    # → 试运行 → 提交发布」）；02 §7 表格回填随文档批。
    WORKFLOW_NOT_PUBLISHED = 4804
    INTERNAL_ERROR = 5999


# ---------------------------------------------------------------- 统一错误体与网关异常


class GatewayError(Exception):
    """网关层业务异常：路由/依赖抛出，由全局异常中间件转统一错误体（02 §7）。"""

    def __init__(
        self,
        code: int | ErrorCode,
        message: str,
        *,
        status_code: int = 400,
        detail: object = None,
    ) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.status_code = status_code
        self.detail = detail


def error_response(
    request: Request,
    code: int | ErrorCode,
    message: str,
    *,
    status_code: int = 400,
    detail: object = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """02 §3 约定：③④⑤⑦ 共用同一构造函数，错误体四字段全局同构。"""
    body = {
        "code": int(code),
        "message": message,
        "detail": detail,
        "trace_id": getattr(request.state, "trace_id", None),
    }
    return JSONResponse(body, status_code=status_code, headers=headers)


# ---------------------------------------------------------------- trace 上下文（02 §3 ② / 08 §1）

trace_id_ctx: ContextVar[str | None] = ContextVar("trace_id", default=None)
tenant_id_ctx: ContextVar[str | None] = ContextVar("tenant_id", default=None)
