"""提示词钉死引用解析器（H-1；standards/01 §5.1「版本钉死 version pin」的纯函数面）。

**运行时钉死语法**：``"prompt:{template_id}@{version}"`` 或 ``"prompt:{template_id}@head"``
（head=当前最新版本；消解后回执一律返回确定版本号——同一 job 全程同版本，回归可复现、
成本可归因）。解析结果携带 checksum（sha256 前 16 hex，与聚合同口径），运行期引用
id@version+checksum 落事件/审计（DB 装配面见 prompt_service.build_db_prompt_resolver）。

本模块为叶子（仅依赖 domain.model.prompt + platform.errors）——builtin 适配器的
一处最小接入（adapters/builtin.py）只 import 本模块，不触 data 层。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from services.agent.domain.model.prompt import FewShotExample, PromptError, PromptVersion, VariableDecl
from services.platform.errors import GatewayError

PROMPT_REF_PREFIX = "prompt:"
HEAD_MARKER = "head"


@dataclass(frozen=True)
class PromptRef:
    """钉死引用解析形：version=None 表示 ``@head``（消解时取当前最新）。"""

    template_id: uuid.UUID
    version: int | None


@dataclass(frozen=True)
class ResolvedPrompt:
    """钉死引用消解回执：确定版本内容 + 规范引用（@head 已消解为具体版本号）+ checksum。"""

    template_id: uuid.UUID
    slug: str
    version: int
    checksum: str
    ref: str  # "prompt:{id}@{version}"（规范形；审计/事件记录的就是它）
    system_prompt: str | None
    template: str | None
    few_shot: tuple[FewShotExample, ...]
    variables: tuple[VariableDecl, ...]


class PromptResolver(Protocol):
    """运行时解析端口（builtin 适配器消费）：ref 字符串 → 消解回执。

    组合根注入 DB 装配（build_db_prompt_resolver）；未装配时 builtin 对 ``prompt:``
    引用 fail-closed（5002 语义）——宁拒不错载。
    """

    async def __call__(self, ref: str) -> ResolvedPrompt: ...


def is_prompt_ref(value: str | None) -> bool:
    """是否钉死引用语法（``^prompt:`` 前缀，api/01 §5.10 消费约定）。"""
    return value is not None and value.startswith(PROMPT_REF_PREFIX)


def parse_prompt_ref(ref: str) -> PromptRef:
    """纯解析（零 IO）：``prompt:{uuid}@{version|head}`` → PromptRef；违式 → 3001。"""
    if not isinstance(ref, str) or not ref.startswith(PROMPT_REF_PREFIX):
        raise PromptError(f"3001 PARAM_INVALID: 非提示词钉死引用（须以 {PROMPT_REF_PREFIX} 开头）: {ref!r}")
    body = ref[len(PROMPT_REF_PREFIX) :]
    if "@" not in body:
        raise PromptError(
            f"3001 PARAM_INVALID: 钉死引用缺 @version 段（语法 prompt:{{id}}@{{version|head}}）: {ref!r}"
        )
    raw_id, _, raw_version = body.partition("@")
    try:
        template_id = uuid.UUID(raw_id)
    except ValueError as exc:
        raise PromptError(f"3001 PARAM_INVALID: 钉死引用 template_id 非 UUID: {raw_id!r}") from exc
    if raw_version == HEAD_MARKER:
        return PromptRef(template_id=template_id, version=None)
    if not raw_version.isdigit() or int(raw_version) < 1:
        raise PromptError(
            f"3001 PARAM_INVALID: 钉死引用版本段 {raw_version!r} 非法（正整数或 {HEAD_MARKER}）"
        )
    return PromptRef(template_id=template_id, version=int(raw_version))


def format_prompt_ref(template_id: uuid.UUID, version: int) -> str:
    """规范引用形（消解回执/审计记录用）：``prompt:{id}@{version}``。"""
    return f"{PROMPT_REF_PREFIX}{template_id}@{version}"


def prompt_error_to_gateway(exc: PromptError) -> GatewayError:
    """PromptError（消息前缀即登记码）→ GatewayError（沿 agents 路由同款映射）。"""
    head = str(exc)[:4].strip()  # "404 " 截断含空格，先剥（agents 同款前缀协议）
    if head == "404":
        return GatewayError(404, str(exc), status_code=404)
    if head == "409":
        return GatewayError(409, str(exc), status_code=409)
    code = int(head) if head.isdigit() else 3001
    return GatewayError(code, str(exc), status_code=400 if 3000 <= code < 4000 else 409)


def resolved_from_version(
    *, template_id: uuid.UUID, slug: str, version: PromptVersion
) -> ResolvedPrompt:
    """聚合版本 → 消解回执（@head 在此消解为确定版本号——版本钉死红线）。"""
    return ResolvedPrompt(
        template_id=template_id,
        slug=slug,
        version=version.version,
        checksum=version.checksum,
        ref=format_prompt_ref(template_id, version.version),
        system_prompt=version.system_prompt,
        template=version.template,
        few_shot=version.few_shot,
        variables=version.variables,
    )


def persona_text(resolved: ResolvedPrompt) -> str:
    """消解回执 → builtin 人格面板文本：优先 system_prompt，退 template（含 {{槽}} 原样）。"""
    if resolved.system_prompt:
        return resolved.system_prompt
    return resolved.template or ""
