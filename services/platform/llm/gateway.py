"""L7 模型网关 · OpenAI 兼容 chat.completions 最小实现（模块 7 / architecture/07 §9）。

- 单渠道直连（本地 vLLM / 云渠道均为 OpenAI 兼容端点，config.llm_base_url/llm_model/llm_api_key）；
  多渠道 fallback 随 foundation/llm 收编（M0 口径）；
- 输出契约：complete_structured 返回已过 JSON Schema 校验的 dict（宪法第 2 条：LLM 输出
  必过确定性校验）；校验优先用 jsonschema 库（未声明依赖，传递可用即用），缺失时退回
  内置最小校验器（本文件 ``_MinimalSchemaValidator``，覆盖抽取输出 schema 用到的子集）；
- 分层约束：infra 禁 import services.domain（import-linter layers 契约），故本文件以
  ``ModelGatewayError`` 族镜像 L4 ``ModelPortError`` 的错误码语义（5001/5002=02 篇 §7 已
  登记），结构化一致性由组合根绑定处（L2 网关 kb.py）按 Protocol 结构化类型核对；
- FakeModelPort：确定性测试桩（无网络），供单测与 kill -9 断点续跑集成用例注入；
  生产绑定只发生在组合根，本模块不自动接线（无 LLM 配置时 extract 步以 5002 失败，不阻塞启动）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping

import httpx

logger = logging.getLogger("services.platform.llm.gateway")

# jsonschema 未列入 pyproject 依赖（禁改 pyproject）；传递可用即用，否则退回内置最小校验。
try:  # pragma: no cover - 环境相关分支
    from jsonschema import Draft202012Validator

    _HAS_JSONSCHEMA = True
except ModuleNotFoundError:  # pragma: no cover
    _HAS_JSONSCHEMA = False

# 用量上下文（llm_calls 审计批量落库的 token 来源；审计装饰器 services/platform/llm/audited.py 消费）
from services.platform.llm.usage import LlmUsage, set_last_usage  # noqa: E402

# ---------------------------------------------------------------- 错误族（镜像 L4 ModelPortError，码值=02 篇 §7）


class ModelGatewayError(RuntimeError):
    """模型网关错误基类：``code`` 为 02 篇 §7 已登记平台错误码，消息带码前缀可追溯。"""

    code = 5999


class ModelGatewayConfigError(ModelGatewayError):
    """构造期配置错误（api_key/base_url 缺失）——组合根须先配置再绑定（5002 同段口径）。"""

    code = 5002


class ModelGatewayTimeoutError(ModelGatewayError):
    """LLM 响应超时（5001 LLM_TIMEOUT）。"""

    code = 5001


class ModelGatewayUnavailableError(ModelGatewayError):
    """LLM 依赖不可用：不可达 / 非 200 / 未配置（5002 LLM_UNAVAILABLE）。"""

    code = 5002


class ModelGatewayOutputInvalidError(ModelGatewayError):
    """LLM 输出不可用：非 JSON / JSON Schema 校验不通过（5002 同段，宪法第 2 条门禁）。"""

    code = 5002


# ---------------------------------------------------------------- JSON Schema 校验（jsonschema 优先，最小校验兜底）


def validate_against_schema(data: object, schema: Mapping[str, object]) -> None:
    """按 JSON Schema 校验数据；不通过抛 ModelGatewayOutputInvalidError（含首条错误消息）。"""
    if _HAS_JSONSCHEMA:
        validator = Draft202012Validator(dict(schema))
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
        if errors:
            first = errors[0]
            path = "/".join(str(p) for p in first.absolute_path) or "$"
            raise ModelGatewayOutputInvalidError(f"输出不符合 JSON Schema（{path}: {first.message}）")
        return
    _MinimalSchemaValidator(schema).validate(data, "$")


class _MinimalSchemaValidator:
    """内置最小 JSON Schema 校验器（jsonschema 库不可用时的兜底）。

    仅覆盖平台结构化输出实际使用的子集：type / required / properties / additionalProperties /
    items / enum / minLength / maxLength / minimum / maximum / minItems / maxItems。
    """

    _TYPES: dict[str, tuple[type, ...]] = {
        "object": (dict,),
        "array": (list,),
        "string": (str,),
        "number": (int, float),
        "integer": (int,),
        "boolean": (bool,),
    }

    def __init__(self, schema: Mapping[str, object]) -> None:
        self._schema = schema

    def validate(self, data: object, path: str) -> None:
        self._check(data, self._schema, path)

    def _fail(self, path: str, message: str) -> None:
        raise ModelGatewayOutputInvalidError(f"输出不符合 JSON Schema（{path}: {message}）")

    def _check(self, data: object, schema: Mapping[str, object], path: str) -> None:
        expected = schema.get("type")
        if isinstance(expected, str) and not isinstance(data, self._TYPES.get(expected, (object,))):
            # bool 是 int 子类，排除 number/integer 对 bool 的误放行
            if not (expected in ("number", "integer") and isinstance(data, bool)):
                self._fail(path, f"期望类型 {expected}，实际 {type(data).__name__}")
        if "enum" in schema and data not in schema["enum"]:  # type: ignore[operator]
            self._fail(path, f"值 {data!r} 不在枚举 {schema['enum']!r} 内")
        if isinstance(data, str):
            if "minLength" in schema and len(data) < schema["minLength"]:  # type: ignore[operator]
                self._fail(path, f"长度 {len(data)} < minLength={schema['minLength']}")
            if "maxLength" in schema and len(data) > schema["maxLength"]:  # type: ignore[operator]
                self._fail(path, f"长度 {len(data)} > maxLength={schema['maxLength']}")
        for key in ("minimum", "maximum"):
            if key in schema and isinstance(data, (int, float)) and not isinstance(data, bool):
                violated = data < schema[key] if key == "minimum" else data > schema[key]  # type: ignore[operator]
                if violated:
                    self._fail(path, f"值 {data} 违反 {key}={schema[key]}")
        if isinstance(data, dict):
            self._check_object(data, schema, path)
        if isinstance(data, list):
            self._check_array(data, schema, path)

    def _check_object(self, data: dict, schema: Mapping[str, object], path: str) -> None:
        required = schema.get("required") or []
        for name in required:  # type: ignore[union-attr]
            if name not in data:
                self._fail(f"{path}/{name}", "必填字段缺失")
        properties = schema.get("properties") or {}
        for key, value in data.items():
            child_schema = properties.get(key) if isinstance(properties, dict) else None  # type: ignore[union-attr]
            if child_schema is None:
                additional = schema.get("additionalProperties", True)
                if additional is False:
                    self._fail(f"{path}/{key}", "额外字段不允许（additionalProperties=false）")
                if isinstance(additional, dict):
                    self._check(value, additional, f"{path}/{key}")
                continue
            self._check(value, child_schema, f"{path}/{key}")  # type: ignore[arg-type]

    def _check_array(self, data: list, schema: Mapping[str, object], path: str) -> None:
        if "minItems" in schema and len(data) < schema["minItems"]:  # type: ignore[operator]
            self._fail(path, f"元素数 {len(data)} < minItems={schema['minItems']}")
        if "maxItems" in schema and len(data) > schema["maxItems"]:  # type: ignore[operator]
            self._fail(path, f"元素数 {len(data)} > maxItems={schema['maxItems']}")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, element in enumerate(data):
                self._check(element, items, f"{path}/{index}")


# ---------------------------------------------------------------- 生产实现（OpenAI 兼容）


def _extract_json(content: str) -> str:
    """思考系模型（qwen3/deepseek-r1）兼容：剥 <think> 块与 markdown 围栏后取 JSON 主体。

    真机冒烟发现（2026-09-27）：qwen3 思考模式在 json_object 约束下仍可能输出
    `<think>…</think>{...}` 或 ```json 围栏——直接 json.loads 失败。策略：
    ①剥思考块与围栏；②仍失败则截取首个 `{` 到最后一个 `}` 再试（由调用方兜底报错）。
    """
    import re

    text = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    if text.startswith("{"):
        return text
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


class OpenAICompatibleModelPort:
    """OpenAI 兼容 /chat/completions 结构化输出客户端（httpx.AsyncClient，超时必设）。

    AsyncClient 可注入复用（组合根/测试挂连接池）；未注入则按 timeout_s 自建。
    任何失败归一为 ModelGatewayError 族（码值=02 篇 §7 已登记段），调用方按码处理。
    provider 属性供审计装饰器落 llm_calls.provider。
    """

    provider = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ModelGatewayConfigError("5002 LLM_UNAVAILABLE: llm_api_key 缺失，模型网关拒绝构造")
        if not base_url:
            raise ModelGatewayConfigError("5002 LLM_UNAVAILABLE: llm_base_url 缺失，模型网关拒绝构造")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s
        self._client = client or httpx.AsyncClient(timeout=timeout_s, headers={"Authorization": f"Bearer {api_key}"})

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict,
        timeout_s: float = 60.0,
        trace_id: str | None = None,
        num_ctx: int | None = None,
    ) -> dict:
        """单次结构化补全：temperature 锁定事实型档 0.1（architecture/03 §3 抽取采样参数）。

        num_ctx：上下文窗口注入参数（可选，向后兼容）——Ollama/vLLM 语义的 num_ctx 原样透传，
        OpenAI 官方端点不识别该字段时由服务端忽略（api/01 §6 无此参数的登记，属透传扩展）。
        调用后回填用量上下文（prompt/completion/cache-read tokens），供 llm_calls 审计消费。
        """
        body: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        if num_ctx is not None:
            body["num_ctx"] = num_ctx
        try:
            resp = await self._client.post(
                f"{self._base_url}/chat/completions", json=body, timeout=httpx.Timeout(timeout_s)
            )
        except httpx.TimeoutException as exc:
            raise ModelGatewayTimeoutError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayUnavailableError(f"模型服务不可达（{self._base_url}）: {exc}") from exc
        if resp.status_code != 200:
            raise ModelGatewayUnavailableError(f"模型服务返回 {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise KeyError("content")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelGatewayUnavailableError(f"响应结构异常（缺 choices/message/content）: {exc}") from exc
        self._capture_usage(payload)
        try:
            data = json.loads(_extract_json(content))
        except ValueError as exc:
            raise ModelGatewayOutputInvalidError(f"输出非合法 JSON: {exc}") from exc
        validate_against_schema(data, json_schema)
        logger.info("llm_call ok: model=%s trace_id=%s", self._model, trace_id)
        return data

    def _capture_usage(self, payload: dict) -> None:
        """回填用量上下文（DeepSeek prompt_cache_hit_tokens / OpenAI prompt_tokens_details 双兼容）。"""
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return
        token_in = usage.get("prompt_tokens")
        token_out = usage.get("completion_tokens")
        cache_read = usage.get("prompt_cache_hit_tokens")
        if not isinstance(cache_read, int):
            details = usage.get("prompt_tokens_details")
            cache_read = details.get("cached_tokens") if isinstance(details, dict) else None
        set_last_usage(
            LlmUsage(
                token_in=token_in if isinstance(token_in, int) else 0,
                token_out=token_out if isinstance(token_out, int) else 0,
                cache_read_tokens=cache_read if isinstance(cache_read, int) else 0,
            )
        )

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------- 确定性测试桩


class FakeModelPort:
    """确定性模型测试桩（无网络）：同输入恒同输出——断点续跑「不重不漏」断言的依据。

    行为：取 user 文本中「## 抽取文本」标记之后的正文，按 keyword_classes 的声明序扫描
    命中关键词，每个命中产出一条实体候选（类名取映射值，属性取 keyword_properties）。
    可注入延迟 / 持续失败 / 第 N 次调用永久挂起（kill -9 用例）/ 调用回调（父子进程同步点）。

    行为口径（与生产 ModelPort 一致，P2-1）：本桩恒产合法输出（`{"candidates": [...]}` 过
    抽取 schema），从不抛 ModelGatewayOutputInvalidError——「校验失败反馈重试 1 次」由
    生产绑定 AuditedModelPort 在装饰器层承担（对任意 inner 实现口径一致），桩无需模拟该路径。
    """

    MARKER = "## 抽取文本"

    def __init__(
        self,
        *,
        keyword_classes: Mapping[str, str] | None = None,
        keyword_properties: Mapping[str, Mapping[str, str]] | None = None,
        delay_s: float = 0.0,
        fail_always_message: str | None = None,
        hang_on_call: int | None = None,
        on_call: Callable[[int], None] | None = None,
    ) -> None:
        self._keyword_classes = dict(keyword_classes or {})
        self._keyword_properties = dict(keyword_properties or {})
        self._delay_s = delay_s
        self._fail_always_message = fail_always_message
        self._hang_on_call = hang_on_call
        self._on_call = on_call
        self.calls = 0

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict,
        timeout_s: float = 60.0,
        trace_id: str | None = None,
        num_ctx: int | None = None,
    ) -> dict:
        self.calls += 1
        if self._on_call is not None:
            self._on_call(self.calls)
        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)
        if self._hang_on_call is not None and self.calls == self._hang_on_call:
            await asyncio.Event().wait()  # 永久挂起：由父进程 kill（Windows=TerminateProcess）
        if self._fail_always_message is not None:
            raise ModelGatewayUnavailableError(self._fail_always_message)
        body = user.split(self.MARKER, 1)[-1]  # 只扫描正文段（本体引导清单不含关键词噪声）
        candidates = [
            {
                "kind": "entity",
                "name": keyword,
                "ontology_class": ontology_class,
                "confidence": 0.9,
                "detail": f"原文提及{keyword}",
                "properties": dict(self._keyword_properties.get(keyword, {})),
            }
            for keyword, ontology_class in self._keyword_classes.items()
            if keyword in body
        ]
        return {"candidates": candidates}
