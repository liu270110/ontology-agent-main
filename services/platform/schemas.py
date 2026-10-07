"""L2 契约通用响应信封 DTO（api/01 §3.1 分页与响应包裹）。

列表响应统一 ``{data: [...], meta: PageMeta}``；非列表响应包裹 ``{data, meta}`` 且
``meta`` 可为空对象（=EmptyMeta）。成功体禁用 ``{code,message,data}`` 旧信封
（2026-09-27 对账反例注记；联调缺陷台账 2026-10-04 B1 批统一收口）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PageMeta(BaseModel):
    """偏移分页 meta（api/01 §3.1：page≥1，page_size 上限随端点既有值）。"""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total: int = Field(ge=0)


class EmptyMeta(BaseModel):
    """非列表响应的空 meta（api/01 §3.1「meta 可为空对象」；序列化恒 {}）。"""

    model_config = ConfigDict(extra="forbid")
