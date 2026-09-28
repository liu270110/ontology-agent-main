"""L4 领域模型：PromptTemplate 聚合（H-1 提示词工程治理批；api/01 §5.10 预登记兑现）。

standards/01 §5.1「提示词是版本化资产」的红线代码化（本文件为聚合纪律唯一写路径）：

- **版本不可变**：``PromptVersion`` 值对象 frozen，只追加不覆写——``draft_new_version``
  追加 head+1，旧版本只读；任何变更（含名字变更）都落新版本；
- **内容长度上限**（上下文预算纪律，Agent 服务设计 §4.1 防超窗）：system_prompt/template
  各 ≤32k、few-shot ≤8 条且每条 input/output ≤4k、变量声明 ≤32 项；
- **checksum**：版本内容 sha256 前 16 hex——运行时钉死引用（``prompt:{id}@{version}``）
  解析回执携带，内容改一字节即失配（版本可复现、成本可归因的载体）；
- **DELETE=archive 软删**（active→archived 终态，不物理删除；全程可追溯）；
- 模板引擎=Jinja2 严格子集：仅 ``{{var}}`` 插值槽（静态提取），槽位必须在变量声明内
  （完备性校验，standards/01 §5.1「静态校验进 CI」的聚合内落点）。

错误消息前缀即登记错误码（沿 agent.py 惯例）：形状/上限/枚举→3001（HTTP 400）；
版本缺失→404；归档态变更→409。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ── 上下文预算上限（聚合内硬校验；改值=行为变更，需过评测回归门禁）──────────────
MAX_SYSTEM_PROMPT_CHARS = 32_768
MAX_TEMPLATE_CHARS = 32_768
MAX_FEWSHOT_ITEMS = 8
MAX_FEWSHOT_PART_CHARS = 4_096
MAX_VARIABLES = 32

_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_VAR_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Jinja2 严格子集：仅 {{ var }} 插值槽（禁循环/分支/过滤器——标准 §5.1 模板引擎约定）
_SLOT_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class PromptError(Exception):
    """领域错误（消息前缀即错误码：3xxx=参数/上限（400）、404=版本缺失、409=归档态变更）。"""


class PromptScope(StrEnum):
    """两级作用域（api/01 §5.10 F-08/X12）：personal=仅 owner 读写；tenant=租户共享。"""

    PERSONAL = "personal"
    TENANT = "tenant"


class PromptStatus(StrEnum):
    """active=可引用可演进；archived=软删终态（DELETE 语义，不可再变更）。"""

    ACTIVE = "active"
    ARCHIVED = "archived"


class VariableDecl(BaseModel):
    """变量声明项（值对象 frozen）：template 槽位的契约面（名/说明/是否必填）。"""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str | None = None
    required: bool = True


class FewShotExample(BaseModel):
    """few-shot 样例（值对象 frozen）：input/output 对（standards/01 §5.1 独立版本化资产的行内形态）。"""

    model_config = ConfigDict(frozen=True)

    input: str
    output: str


def compute_checksum(
    *,
    system_prompt: str | None,
    template: str | None,
    few_shot: tuple[FewShotExample, ...] | list[FewShotExample],
    variables: tuple[VariableDecl, ...] | list[VariableDecl],
) -> str:
    """版本内容 checksum（sha256 前 16 hex）：canonical JSON（键排序/紧凑分隔）后哈希。

    运行时钉死解析（resolve_pinned）回执携带同口径值——失配即「引用的版本内容已变」
    （正常不可能发生：版本行只插入不更新；校验是防篡改/防错载的确定性防线）。
    """
    canonical = json.dumps(
        {
            "system_prompt": system_prompt,
            "template": template,
            "few_shot": [{"input": ex.input, "output": ex.output} for ex in few_shot],
            "variables": [
                {"name": v.name, "description": v.description, "required": v.required} for v in variables
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _validate_content(
    *,
    system_prompt: str | None,
    template: str | None,
    few_shot: tuple[FewShotExample, ...],
    variables: tuple[VariableDecl, ...],
) -> None:
    """内容形状与预算上限校验（violation → PromptError 3001；推理分级宪法的确定性校验面）。"""
    if system_prompt is not None and len(system_prompt) > MAX_SYSTEM_PROMPT_CHARS:
        raise PromptError(f"3001 PARAM_INVALID: system_prompt 超长（≤{MAX_SYSTEM_PROMPT_CHARS}，上下文预算纪律）")
    if template is not None and len(template) > MAX_TEMPLATE_CHARS:
        raise PromptError(f"3001 PARAM_INVALID: template 超长（≤{MAX_TEMPLATE_CHARS}，上下文预算纪律）")
    if system_prompt is None and template is None:
        raise PromptError("3001 PARAM_INVALID: system_prompt 与 template 至少提供一项")
    if len(few_shot) > MAX_FEWSHOT_ITEMS:
        raise PromptError(f"3001 PARAM_INVALID: few_shot 至多 {MAX_FEWSHOT_ITEMS} 条")
    for ex in few_shot:
        if len(ex.input) > MAX_FEWSHOT_PART_CHARS or len(ex.output) > MAX_FEWSHOT_PART_CHARS:
            raise PromptError(f"3001 PARAM_INVALID: few_shot 单条 input/output ≤{MAX_FEWSHOT_PART_CHARS}")
    if len(variables) > MAX_VARIABLES:
        raise PromptError(f"3001 PARAM_INVALID: variables 至多 {MAX_VARIABLES} 项")
    seen: set[str] = set()
    for var in variables:
        if not _VAR_NAME_PATTERN.match(var.name):
            raise PromptError(f"3001 PARAM_INVALID: 变量名 {var.name!r} 非法（^[A-Za-z_][A-Za-z0-9_]{{0,63}}$）")
        if var.name in seen:
            raise PromptError(f"3001 PARAM_INVALID: 变量名重复声明 {var.name!r}")
        seen.add(var.name)
    if template is not None:
        undeclared = sorted(set(_SLOT_PATTERN.findall(template)) - seen)
        if undeclared:
            raise PromptError(
                f"3001 PARAM_INVALID: template 槽位 {undeclared} 未在 variables 声明（完备性校验，standards/01 §5.1）"
            )


class PromptVersion(BaseModel):
    """提示词版本（值对象 frozen——版本不可变红线）：内容 + checksum + 溯源。

    一个版本=确定的一份提示词资产：``system_prompt``（人格/指令全文）或 ``template``
    （带 ``{{变量}}`` 槽的模板）至少其一；``few_shot``/``variables`` 为行内配套资产。
    """

    model_config = ConfigDict(frozen=True)

    version: int  # 严格递增，1 起
    system_prompt: str | None = None
    template: str | None = None
    few_shot: tuple[FewShotExample, ...] = ()
    variables: tuple[VariableDecl, ...] = ()
    checksum: str
    created_by: uuid.UUID
    created_at: datetime | None = None  # 仓储回填，聚合内不消费

    @property
    def template_slots(self) -> tuple[str, ...]:
        """模板槽位静态提取（确定性、零 LLM）。"""
        return tuple(_SLOT_PATTERN.findall(self.template)) if self.template else ()


class PromptTemplate(BaseModel):
    """PromptTemplate 聚合根：一条两级作用域（personal/tenant）的提示词模板及其版本链。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    owner_user_id: uuid.UUID
    scope: PromptScope
    slug: str
    name: str
    status: PromptStatus = PromptStatus.ACTIVE
    versions: list[PromptVersion] = Field(default_factory=list)
    created_at: datetime | None = None  # 仓储回填

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PromptTemplate) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    # ── 工厂（POST /prompts：创建即含首版本 v1）──────────────────────────────
    @classmethod
    def create(
        cls,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        scope: PromptScope | str,
        slug: str,
        name: str,
        system_prompt: str | None = None,
        template: str | None = None,
        few_shot: list[FewShotExample] | tuple[FewShotExample, ...] = (),
        variables: list[VariableDecl] | tuple[VariableDecl, ...] = (),
        created_by: uuid.UUID,
    ) -> PromptTemplate:
        """创建模板并落首版本 v1：作用域/命名/内容预算全量校验（violation→PromptError）。"""
        try:
            scope_enum = PromptScope(scope)
        except ValueError as exc:
            raise PromptError(f"3001 PARAM_INVALID: scope={scope} 非法（允许 personal|tenant）") from exc
        if not _SLUG_PATTERN.match(slug or ""):
            raise PromptError("3001 PARAM_INVALID: slug 非法（^[a-z0-9][a-z0-9._-]{0,127}$，机器可引用名）")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 128:
            raise PromptError("3001 PARAM_INVALID: name 须为 1~128 字符")
        few = tuple(few_shot)
        decls = tuple(variables)
        _validate_content(system_prompt=system_prompt, template=template, few_shot=few, variables=decls)
        self = cls(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            scope=scope_enum,
            slug=slug,
            name=name.strip(),
            versions=[
                PromptVersion(
                    version=1,
                    system_prompt=system_prompt,
                    template=template,
                    few_shot=few,
                    variables=decls,
                    checksum=compute_checksum(
                        system_prompt=system_prompt, template=template, few_shot=few, variables=decls
                    ),
                    created_by=created_by,
                )
            ],
        )
        return self

    # ── 版本演进（PUT /prompts/{id}：语义=追加新版本）─────────────────────────
    def draft_new_version(
        self,
        *,
        name: str | None = None,
        system_prompt: str | None = None,
        template: str | None = None,
        few_shot: list[FewShotExample] | tuple[FewShotExample, ...] | None = None,
        variables: list[VariableDecl] | tuple[VariableDecl, ...] | None = None,
        created_by: uuid.UUID,
    ) -> PromptVersion:
        """追加新版本（版本不可变红线：本方法只 append，旧版本永不改写）。

        部分更新语义：None=承接 head 同字段（few_shot/variables 整组替换或整组承接）；
        名字变更同批生效——任何变更（名字/内容）都落新版本，200 返回新版本号。
        archived 终态禁止变更（DELETE 语义后不可复活，409）。
        """
        if self.status is PromptStatus.ARCHIVED:
            raise PromptError("409 PROMPT_ARCHIVED: 模板已归档（DELETE 语义），禁止追加版本")
        if name is not None:
            if not name.strip() or len(name.strip()) > 128:
                raise PromptError("3001 PARAM_INVALID: name 须为 1~128 字符")
        head = self.head
        merged_prompt = head.system_prompt if system_prompt is None else system_prompt
        merged_template = head.template if template is None else template
        merged_few = head.few_shot if few_shot is None else tuple(few_shot)
        merged_vars = head.variables if variables is None else tuple(variables)
        _validate_content(
            system_prompt=merged_prompt, template=merged_template, few_shot=merged_few, variables=merged_vars
        )
        if name is not None:
            self.name = name.strip()
        version = PromptVersion(
            version=head.version + 1,
            system_prompt=merged_prompt,
            template=merged_template,
            few_shot=merged_few,
            variables=merged_vars,
            checksum=compute_checksum(
                system_prompt=merged_prompt, template=merged_template, few_shot=merged_few, variables=merged_vars
            ),
            created_by=created_by,
        )
        self.versions.append(version)
        return version

    # ── 软删（DELETE /prompts/{id}=archive，终态）────────────────────────────
    def archive(self) -> None:
        """归档（active→archived 终态）：软删不物理删除，版本链保留可追溯。"""
        if self.status is PromptStatus.ARCHIVED:
            raise PromptError("409 PROMPT_ARCHIVED: 模板已归档，勿重复删除")
        self.status = PromptStatus.ARCHIVED

    # ── 解析（运行时钉死引用的消费面；纯确定性，零 LLM）──────────────────────
    @property
    def head(self) -> PromptVersion:
        """最新版本（无版本=聚合破损，防御性结构化错误）。"""
        if not self.versions:
            raise PromptError(f"404 PROMPT_VERSION_MISSING: 模板 {self.id} 无任何版本（数据破损，联系管理员）")
        return self.versions[-1]

    def resolve(self, version: int) -> PromptVersion:
        """取确定版本：缺失 → 结构化错误（含可用范围，调用方映射 404）。"""
        for v in self.versions:
            if v.version == version:
                return v
        lo = self.versions[0].version if self.versions else 1
        raise PromptError(
            f"404 PROMPT_VERSION_MISSING: 模板 {self.id} 无版本 {version}"
            f"（可用 {lo}~{self.head.version}；钉死引用须指向已存在版本，standards/01 §5.1 版本钉死）"
        )

    def ensure_usable(self) -> None:
        """不变式：archived 模板不得被运行时引用（解析入口强制，PromptError→409）。"""
        if self.status is PromptStatus.ARCHIVED:
            raise PromptError(f"409 PROMPT_ARCHIVED: 模板 {self.slug} 已归档，禁止运行时引用")


def version_content_to_domain(
    content: dict[str, Any],
) -> tuple[str | None, str | None, tuple[FewShotExample, ...], tuple[VariableDecl, ...]]:
    """版本行 JSONB content → 领域四元组（repo_impl 装配用；形状收窄，值不经采样）。"""
    few = tuple(FewShotExample(**ex) for ex in content.get("few_shot") or [])
    decls = tuple(VariableDecl(**d) for d in content.get("variables") or [])
    return content.get("system_prompt"), content.get("template"), few, decls


def version_content_from_domain(version: PromptVersion) -> dict[str, Any]:
    """领域版本 → 版本行 JSONB content（repo_impl 落库用）。"""
    return {
        "system_prompt": version.system_prompt,
        "template": version.template,
        "few_shot": [{"input": ex.input, "output": ex.output} for ex in version.few_shot],
        "variables": [
            {"name": v.name, "description": v.description, "required": v.required} for v in version.variables
        ],
    }
