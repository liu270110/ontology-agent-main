"""kb 提示词模板注册表（standards/01 §5.1 / 18 篇 §1.1：提示词是版本化资产，版本钉死）。

- ``PROMPTS``：template_ref（"模板 id@version"）→ 用户提示词渲染函数；
  ``SYSTEM_PROMPTS``：template_ref → 系统提示词正文。两表同键集，ref 即版本坐标。
- 运行期只经本注册表按 ref 取用（version pin）：未知 ref 明确抛
  :class:`UnknownTemplateRefError`，禁止静默回退到其他版本——回归可复现、成本可归因。
- 新增版本 = 新增模块（如 ``extract_v2.py``）+ 两表登记；active 版本正文不可变，
  变更走评审（快照断言即回归基线，tests/kb/test_prompt_versioning.py）。
"""

from __future__ import annotations

from collections.abc import Callable

from services.kb.business.prompts import extract_v1, extract_v2, extract_v3, extract_v4


class UnknownTemplateRefError(KeyError):
    """注册表未登记的 template_ref（治理要求：模板必须版本化登记后方可取用）。"""


PROMPTS: dict[str, Callable[..., str]] = {
    extract_v1.TEMPLATE_REF: extract_v1.render,
    extract_v2.TEMPLATE_REF: extract_v2.render,
    extract_v3.TEMPLATE_REF: extract_v3.render,
    extract_v4.TEMPLATE_REF: extract_v4.render,
}

SYSTEM_PROMPTS: dict[str, str] = {
    extract_v1.TEMPLATE_REF: extract_v1.SYSTEM_PROMPT,
    extract_v2.TEMPLATE_REF: extract_v2.SYSTEM_PROMPT,
    extract_v3.TEMPLATE_REF: extract_v3.SYSTEM_PROMPT,
    extract_v4.TEMPLATE_REF: extract_v4.SYSTEM_PROMPT,
}


def get_prompt(template_ref: str) -> Callable[..., str]:
    """按 ref 取用户提示词渲染函数；未登记 ref 抛 UnknownTemplateRefError（不静默回退）。"""
    try:
        return PROMPTS[template_ref]
    except KeyError:
        raise UnknownTemplateRefError(
            f"未登记的提示词模板 ref: {template_ref!r}（已注册: {sorted(PROMPTS)}）"
        ) from None


def get_system_prompt(template_ref: str) -> str:
    """按 ref 取系统提示词正文；未登记 ref 抛 UnknownTemplateRefError（不静默回退）。"""
    try:
        return SYSTEM_PROMPTS[template_ref]
    except KeyError:
        raise UnknownTemplateRefError(
            f"未登记的系统提示词 ref: {template_ref!r}（已注册: {sorted(SYSTEM_PROMPTS)}）"
        ) from None
