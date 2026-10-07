"""L5 ontology_core · diff：发布版本读模型结构化差异（本体核心设计 §6.2 diff 语义分组口径）。

输入 = 两个版本的读模型投影（L5 projection 产物）；输出 = 类/属性/公理/规则四组
added/removed/modified 清单（key 定位 + 字段级 before/after）。确定性排序（按 key），零存储/HTTP 依赖；
不做 NL 解释（评审界面分块展示归前端，§6.2 ③ 语义分组即本模块的分组粒度）。

口径注记：三元组级 RDFC-1.0 规范化 diff（§6.2 ①②）在制品层可做集合差，但读模型层 diff 直接
以投影行 IRI/名称为键——免规范化、天然按 OB2 要素分组，且与工作台四表同构（最小闭环裁决，
登记于 api/01 §5.3 diff 行实现注记；三元组级 diff 待 rebase 机制一并细化，§11 待办）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from services.ontology.domain.model.ontology_read_model import (
    ReadModelAxiom,
    ReadModelClass,
    ReadModelProjection,
    ReadModelProperty,
    ReadModelRule,
)

_T = TypeVar("_T", ReadModelClass, ReadModelProperty, ReadModelAxiom, ReadModelRule)


class FieldChange(BaseModel):
    """字段级差异（before/after 为字段原值；JSON 安全类型——读模型字段全部 JSONB 同构）。"""

    field: str
    before: Any = None
    after: Any = None


class DiffEntry(BaseModel):
    """单个差异项：key=定位标识（类/属性 IRI、规则名、公理表达式），modified 附字段级 changes。"""

    key: str
    label: str | None = None  # 人类可读名（类/属性本地名、公理 kind 等）
    changes: list[FieldChange] = Field(default_factory=list)


class ElementDiff(BaseModel):
    """单要素分组差异（classes/properties/axioms/rules 各一组）。"""

    added: list[DiffEntry] = Field(default_factory=list)
    removed: list[DiffEntry] = Field(default_factory=list)
    modified: list[DiffEntry] = Field(default_factory=list)
    unchanged: int = 0


class ProjectionDiff(BaseModel):
    """两版本读模型差异全量报告（确定性排序；summary=扁平计数供列表页徽标）。"""

    base_version: str
    target_version: str
    classes: ElementDiff = Field(default_factory=ElementDiff)
    properties: ElementDiff = Field(default_factory=ElementDiff)
    axioms: ElementDiff = Field(default_factory=ElementDiff)
    rules: ElementDiff = Field(default_factory=ElementDiff)
    summary: dict[str, int] = Field(default_factory=dict)


def diff_projections(
    base: ReadModelProjection, target: ReadModelProjection, *, base_version: str, target_version: str
) -> ProjectionDiff:
    """两版本读模型 → 结构化差异（类/属性按 IRI、规则按版本内唯一名称、公理按三元组定位）。"""
    classes = _diff_elements(base.classes, target.classes, key=lambda c: c.iri, label=lambda c: c.name)
    properties = _diff_elements(base.properties, target.properties, key=lambda p: p.iri, label=lambda p: p.name)
    rules = _diff_elements(base.rules, target.rules, key=lambda r: r.name, label=lambda r: r.name)
    axioms = _diff_elements(
        base.axioms,
        target.axioms,
        key=lambda a: f"{a.subject_iri} {a.kind} {a.object_iri}",
        label=lambda a: a.kind,
    )
    diff = ProjectionDiff(
        base_version=base_version,
        target_version=target_version,
        classes=classes,
        properties=properties,
        axioms=axioms,
        rules=rules,
    )
    # summary = 扁平计数（{group}_{bucket} → 条数），列表页徽标直接消费
    diff.summary = {
        f"{group}_{bucket}": len(getattr(getattr(diff, group), bucket))
        for group in ("classes", "properties", "axioms", "rules")
        for bucket in ("added", "removed", "modified")
    }
    return diff


def _diff_elements(
    base_items: list[_T],
    target_items: list[_T],
    *,
    key: Callable[[_T], str],
    label: Callable[[_T], str | None],
) -> ElementDiff:
    """按 key 分组差集：added/removed 全项列出，modified 逐字段 before/after，确定性按 key 排序。"""
    base_map = {key(item): item for item in base_items}
    target_map = {key(item): item for item in target_items}
    added = sorted(target_map.keys() - base_map.keys())
    removed = sorted(base_map.keys() - target_map.keys())
    modified: list[DiffEntry] = []
    unchanged = 0
    for k in sorted(base_map.keys() & target_map.keys()):
        changes = _field_changes(base_map[k], target_map[k])
        if changes:
            modified.append(DiffEntry(key=k, label=label(target_map[k]), changes=changes))
        else:
            unchanged += 1
    return ElementDiff(
        added=[DiffEntry(key=k, label=label(target_map[k])) for k in added],
        removed=[DiffEntry(key=k, label=label(base_map[k])) for k in removed],
        modified=modified,
        unchanged=unchanged,
    )


def _field_changes(before: _T, after: _T) -> list[FieldChange]:
    before_dump, after_dump = before.model_dump(), after.model_dump()
    return [
        FieldChange(field=field, before=before_dump[field], after=after_dump[field])
        for field in sorted(before_dump)
        if before_dump[field] != after_dump[field]
    ]
