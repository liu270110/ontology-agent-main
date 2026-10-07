"""L2 网关 DTO：PromptTemplate（与领域模型严格分离，转换函数同文件；api/01 §5.10）。

铁律：extra="forbid"、snake_case、只数据无行为；DTO 收窄是第一道防线，聚合校验
（预算上限/槽位完备性）是权威防线（双闸，standards/01 §5.1 静态校验）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from services.agent.domain.model.prompt import FewShotExample, PromptTemplate, PromptVersion, VariableDecl

_SCOPE_PATTERN = "^(personal|tenant)$"
_SLUG_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,127}$"


class FewShotIn(BaseModel):
    """few-shot 样例（input/output 对；上限与聚合同源——DTO 先拒，聚合兜底）。"""

    model_config = ConfigDict(extra="forbid")

    input: str = Field(max_length=4_096)
    output: str = Field(max_length=4_096)


class VariableIn(BaseModel):
    """变量声明项（template 槽位契约）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    description: str | None = Field(default=None, max_length=512)
    required: bool = True


class PromptCreateIn(BaseModel):
    """POST /prompts：创建（含首版本 v1）；scope 两级=personal|tenant（api/01 §5.10 F-08/X12）。"""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(pattern=_SCOPE_PATTERN)
    slug: str = Field(min_length=1, max_length=128, pattern=_SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    system_prompt: str | None = Field(default=None, max_length=32_768)
    template: str | None = Field(default=None, max_length=32_768)
    few_shot: list[FewShotIn] = Field(default_factory=list, max_length=8)
    variables: list[VariableIn] = Field(default_factory=list, max_length=32)


class PromptUpdateIn(BaseModel):
    """PUT /prompts/{id}：语义=追加新版本（api/01 §5.10）——名字/内容任一变更都落新版本。

    None=承接 head 同字段（部分更新）；few_shot/variables 给定即整组替换。
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    system_prompt: str | None = Field(default=None, max_length=32_768)
    template: str | None = Field(default=None, max_length=32_768)
    few_shot: list[FewShotIn] | None = Field(default=None, max_length=8)
    variables: list[VariableIn] | None = Field(default=None, max_length=32)


class FewShotOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str
    output: str


class VariableOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    required: bool


class PromptVersionOut(BaseModel):
    """版本树节点（值对象投影：内容 + checksum + 溯源）。"""

    model_config = ConfigDict(extra="forbid")

    version: int
    system_prompt: str | None = None
    template: str | None = None
    few_shot: list[FewShotOut]
    variables: list[VariableOut]
    checksum: str
    created_by: uuid.UUID
    created_at: datetime | None = None


class PromptOut(BaseModel):
    """列表项（不含版本内容——列表页不装载全文，防大载荷）。"""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    scope: str
    slug: str
    name: str
    status: str
    owner_user_id: uuid.UUID
    head_version: int
    version_count: int
    created_at: datetime | None = None


class PromptDetailOut(PromptOut):
    """详情（含版本树）+ 钉死引用语法面（meta 消费提示）。"""

    model_config = ConfigDict(extra="forbid")

    versions: list[PromptVersionOut]


def _few_from_domain(items: tuple[FewShotExample, ...]) -> list[FewShotOut]:
    return [FewShotOut(input=ex.input, output=ex.output) for ex in items]


def _vars_from_domain(items: tuple[VariableDecl, ...]) -> list[VariableOut]:
    return [VariableOut(name=v.name, description=v.description, required=v.required) for v in items]


def version_from_domain(v: PromptVersion) -> PromptVersionOut:
    return PromptVersionOut(
        version=v.version,
        system_prompt=v.system_prompt,
        template=v.template,
        few_shot=_few_from_domain(v.few_shot),
        variables=_vars_from_domain(v.variables),
        checksum=v.checksum,
        created_by=v.created_by,
        created_at=v.created_at,
    )


def prompt_from_domain(t: PromptTemplate) -> PromptOut:
    head = t.head
    return PromptOut(
        id=t.id,
        scope=t.scope.value,
        slug=t.slug,
        name=t.name,
        status=t.status.value,
        owner_user_id=t.owner_user_id,
        head_version=head.version,
        version_count=len(t.versions),
        created_at=t.created_at,
    )


def prompt_detail_from_domain(t: PromptTemplate) -> PromptDetailOut:
    return PromptDetailOut(
        id=t.id,
        scope=t.scope.value,
        slug=t.slug,
        name=t.name,
        status=t.status.value,
        owner_user_id=t.owner_user_id,
        head_version=t.head.version,
        version_count=len(t.versions),
        created_at=t.created_at,
        versions=[version_from_domain(v) for v in t.versions],
    )


def few_shot_to_domain(items: list[FewShotIn] | None) -> list[FewShotExample]:
    return [FewShotExample(input=x.input, output=x.output) for x in items or []]


def variables_to_domain(items: list[VariableIn] | None) -> list[VariableDecl]:
    return [VariableDecl(name=x.name, description=x.description, required=x.required) for x in items or []]
