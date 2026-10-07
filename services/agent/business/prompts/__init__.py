"""H-1 提示词工程治理批（2026-09-29）：模板库用例 + 运行时钉死解析。

- ``prompt_service``：PromptLibraryService（api/01 §5.10 五端点用例）+ build_db_prompt_resolver；
- ``resolver``：钉死语法 ``prompt:{id}@{version|head}`` 纯解析器 + PromptResolver 端口
  （builtin 适配器唯一接入点，叶子模块零 data 依赖）。
"""

from services.agent.business.prompts.prompt_service import (
    PromptLibraryService,
    build_db_prompt_resolver,
)
from services.agent.business.prompts.resolver import (
    PROMPT_REF_PREFIX,
    PromptRef,
    PromptResolver,
    ResolvedPrompt,
    format_prompt_ref,
    is_prompt_ref,
    parse_prompt_ref,
)

__all__ = [
    "PROMPT_REF_PREFIX",
    "PromptLibraryService",
    "PromptRef",
    "PromptResolver",
    "ResolvedPrompt",
    "build_db_prompt_resolver",
    "format_prompt_ref",
    "is_prompt_ref",
    "parse_prompt_ref",
]
