"""技术能力端口 · ModelPort（锚点 01 §3.4 倒置例外三；standards/01 §2.1 端口命名纪律）。

依赖倒置：L4 只定义 Protocol（纯 Python、零框架依赖），实现由 L5/L7 提供
（kb 抽取流水线 = services/infra/llm/gateway.py 的 OpenAI 兼容客户端），绑定只发生在
组合根（L2 网关）——L3 business 仅依赖本端口，禁直连 infra（standards/01 §2.1 契约⑥）。

推理分级宪法（第 2 条）：本端口只承载「低频语义判断」调用；输出必须已过确定性校验
（JSON Schema）才可返回——schema 校验是实现方责任，调用方拿到即合法结构化产物。
"""

from __future__ import annotations

from typing import Any, Protocol


class ModelPort(Protocol):
    """结构化补全端口（LLM 语义能力唯一入口）。

    契约：
    - 返回值已按 ``json_schema`` 完成校验，可直接当 dict 消费；
    - 任何失败（超时/不可达/未配置/输出不合法）抛 :class:`ModelPortError` 族，
      错误码取自 02 篇 §7 已登记 5xxx 段，禁止新编；
    - 实现必须显式设超时（standards/01 §2.5：一切外部调用 timeout 必设）。
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
        """执行一次结构化补全：system/user 提示 + JSON Schema 约束 → 合法 dict。

        trace_id 贯穿调用链（08 篇 §1 可追溯底线），实现方须透传至日志/审计；
        num_ctx：上下文窗口注入参数（计划 3.3 增参，可选/向后兼容）——Ollama/vLLM 语义
        的上下文窗口大小，实现方透传给模型端点，端点不支持时忽略；None=用实现默认。
        """
        ...  # pragma: no cover — Protocol 方法无实现


class ModelPortError(Exception):
    """模型端口领域错误基类（standards/01 §2.6：异常带平台错误码；5xxx 段=02 篇 §7）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class ModelTimeoutError(ModelPortError):
    """LLM 响应超时（已登记 5001 LLM_TIMEOUT）。"""

    def __init__(self, message: str = "LLM 响应超时") -> None:
        super().__init__(5001, f"5001 LLM_TIMEOUT: {message}")


class ModelUnavailableError(ModelPortError):
    """LLM 依赖不可用（已登记 5002 LLM_UNAVAILABLE）：不可达 / 未配置 / 输出结构不可用。"""

    def __init__(self, message: str = "LLM 不可用") -> None:
        super().__init__(5002, f"5002 LLM_UNAVAILABLE: {message}")
