"""技术能力端口 · ModelPort（锚点 01 §3.4 倒置例外三；standards/01 §2.1 端口命名纪律）。

依赖倒置：L4 只定义 Protocol（纯 Python、零框架依赖），实现由 L5/L7 提供
（kb 抽取流水线 = services/infra/llm/gateway.py 的 OpenAI 兼容客户端），绑定只发生在
组合根（L2 网关）——L3 business 仅依赖本端口，禁直连 infra（standards/01 §2.1 契约⑥）。

双面口径（H-6 模型协议主干批，2026-09-29 用户裁决 OpenAI 协议先行）：
- 结构化判断面 ``complete_structured``：低频语义判断（推理分级宪法第 2 条），输出必须
  已过确定性校验（JSON Schema）才可返回——schema 校验是实现方责任，调用方拿到即合法
  结构化产物；
- 对话生成面 ``complete`` / ``stream_complete``：chat 生成通道（builtin 适配器）的裸文本
  补全（非 JSON 模式）与真流式（HTTP SSE 逐段产出）；产物为散文回答，不走红毯校验，
  语义收口由消费侧事件契约承担（TEXT_MESSAGE_CONTENT 的 delta 形态）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ModelStreamPiece:
    """结构化流式产出单元（reasoning 透传批，2026-10-07）：一段增量可含回答/推理两路。

    - ``content``：回答增量（OpenAI 兼容 ``choices[0].delta.content``）；
    - ``reasoning``：推理增量（vLLM/DeepSeek ``choices[0].delta.reasoning_content``）；
    - 两字段可并存（单 chunk 双路时），空段（双空）由实现方跳过不产出。
    纯 dataclass（本模块零框架依赖纪律）；消费方：builtin 适配器投影为
    reasoning_delta/text_delta 两路 GenerationEvent（并存互不干扰，reasoning 先于 content）。
    """

    content: str = ""
    reasoning: str = ""


class ModelPort(Protocol):
    """LLM 语义能力唯一入口（结构化判断面 + 对话生成面）。

    契约：
    - complete_structured 返回值已按 ``json_schema`` 完成校验，可直接当 dict 消费；
    - 任何失败（超时/不可达/未配置/输出不合法）抛 :class:`ModelPortError` 族，
      错误码取自 02 篇 §7 已登记 5xxx 段，禁止新编；
    - 实现必须显式设超时（standards/01 §2.5：一切外部调用 timeout 必设）——
      ``timeout_s=None`` 语义为「用实现默认（构造期）」，非不设。
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

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        timeout_s: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> str:
        """执行一次裸对话补全（chat completions 文本生成，非 JSON 模式）→ 全文。

        - messages：OpenAI chat 形态（role/content dict 列表），调用方负责组装；
        - temperature/max_tokens=None=实现方默认；num_ctx 同 complete_structured
          （Ollama/vLLM 语义透传，端点不识别则忽略）；timeout_s=None=实现默认（必设）；
        - tools/tool_choice：ReAct 双模式前置件（docs/Agent/02 §11.3）——原样透传
          请求体（Ollama 兼容层忽略不识别字段），本期平台无消费方，仅预留协议面；
        - 错误族契约同 complete_structured（5001 超时 / 5002 不可达·结构异常）。
        """
        ...  # pragma: no cover — Protocol 方法无实现

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        timeout_s: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> AsyncIterator[str]:
        """真流式补全：HTTP stream=True，逐段 yield 文本增量（实现为 async generator）。

        与 complete 同参同契约；SSE 逐行解析（``data: {…choices[0].delta.content…}``
        取增量，``data: [DONE]`` 终止），产出顺序=模型产出顺序、空段不产出——实现必须
        逐段透传，禁止攒齐全文再切片（伪流式）；硬上限由消费侧内核超时钳制，传输层
        超时=timeout_s（每读一次的粒度，None=实现默认，必设）。
        本面为纯文本投影：上游若附带推理增量（``delta.reasoning_content``）在此**丢弃**
        ——推理透传走 :meth:`stream_complete_events`（reasoning 透传批，2026-10-07）。
        """
        ...  # pragma: no cover — Protocol 方法无实现

    def stream_complete_events(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        timeout_s: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> AsyncIterator[ModelStreamPiece]:
        """真流式补全·结构化面（reasoning 透传批，2026-10-07）：逐段 yield ModelStreamPiece。

        可选扩展面：与 stream_complete 同参同契约（SSE 解析/超时/错误码族一致），区别仅在
        产出单元——content 与 reasoning 两路并存透传（空段不产出、顺序=模型产出顺序）；
        装饰器（audited/resilience）与实现方（OpenAICompatibleModelPort）应实现本面，
        旧实现/测试桩可缺席——消费侧（builtin 适配器）``getattr`` 探测，缺席时回退
        stream_complete 纯文本面（reasoning 丢弃，与既有口径一致）。
        """
        ...  # pragma: no cover — Protocol 方法无实现


class ModelPortError(Exception):
    """模型端口领域错误基类（standards/01 §2.6：异常带平台错误码；5xxx 段=02 篇 §7）。

    status_code：上游 HTTP 状态码（实现方拿到响应状态时透传——4xx 客户端错误=确定性
    失败 vs 瞬时错误的调用级重试判定输入，resilience._is_transient_error 鸭型读取；
    无 HTTP 语义（超时/不可达/未配置/输出不合法）恒 None）。
    """

    def __init__(self, code: int, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ModelTimeoutError(ModelPortError):
    """LLM 响应超时（已登记 5001 LLM_TIMEOUT）。"""

    def __init__(self, message: str = "LLM 响应超时") -> None:
        super().__init__(5001, f"5001 LLM_TIMEOUT: {message}")


class ModelUnavailableError(ModelPortError):
    """LLM 依赖不可用（已登记 5002 LLM_UNAVAILABLE）：不可达 / 未配置 / 输出结构不可用。"""

    def __init__(self, message: str = "LLM 不可用") -> None:
        super().__init__(5002, f"5002 LLM_UNAVAILABLE: {message}")
