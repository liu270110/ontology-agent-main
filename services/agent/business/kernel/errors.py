"""内核错误族（standards/01 §2.6：异常带平台错误码；码一律取 platform/errors.ErrorCode 已登记段，禁随手编）。

内核自身不定义新错误码：预算耗尽复用 5005 RETRY_BUDGET_EXHAUSTED（登记表内唯一预算码），
参数/行动类非法复用 3001 PARAM_INVALID，授权不足复用 2001 SCOPE_INSUFFICIENT，
工具目标不可用/超时复用 5003 MCP_TARGET_UNAVAILABLE。
"""

from __future__ import annotations

from services.platform.errors import ErrorCode


class KernelError(Exception):
    """内核错误基类：code 必须来自 ErrorCode 登记段。"""

    def __init__(self, code: ErrorCode | int, message: str) -> None:
        super().__init__(f"{int(code)} {message}")
        self.code = int(code)
        self.message = message


class KernelContractError(KernelError):
    """扩展点契约违例（02 §4.1 违反即拒载/拒注册；§7.4 无语义标注不上架）。"""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.PARAM_INVALID, f"内核契约违例: {message}")


class BudgetExhaustedError(KernelError):
    """A4 预算耗尽（token/步数/时长任一维）；终止只认预算与判据，不认模型自述。"""

    def __init__(self, dimension: str) -> None:
        super().__init__(ErrorCode.RETRY_BUDGET_EXHAUSTED, f"RETRY_BUDGET_EXHAUSTED: 运行预算耗尽（{dimension}）")
        self.dimension = dimension


class ToolDispatchError(KernelError):
    """工具分发失败（超时/不可达，已按结构化 ToolResult 收敛前的内核侧错误）。"""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.MCP_TARGET_UNAVAILABLE, f"MCP_TARGET_UNAVAILABLE: {message}")
