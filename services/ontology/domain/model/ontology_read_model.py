"""L4 领域模型：发布读模型投影值对象（database/01 §3.3 四读模型表；本体核心设计 §7 API 读侧）。

定位：publish 投影用例（L3 changeset_service）与仓储（L4 Protocol / L6 实现）之间的载荷契约——
L5 ontology_core.projection 负责从 TBox 图提取本结构，L6 落 PG classes/properties/axioms/rules 四表。
列约束（NOT NULL/default/check）与 m2_semantic_review.py ORM 一一对应；此处只做形状，无业务不变式。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ReadModelClass(BaseModel):
    """classes 行（uk_classes_version_id_iri：版本内 IRI 唯一）。"""

    iri: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=128)  # 本地标识符（R007 式编号同源）
    label: str | None = None
    definition: str | None = None
    subclass_of: list[str] = Field(default_factory=list)  # 父类 IRI（JSONB）
    equivalent_class: list[str] = Field(default_factory=list)
    is_behavior: bool = False  # OB2 行动类（subClassOf* ob2:Action）
    state_attribute: dict[str, Any] | None = None  # 状态流转属性（如 hasStatus 枚举）
    metadata: dict[str, Any] = Field(default_factory=dict)  # 国标附录 A 8 项（M2 仅 source 留痕）


class ReadModelProperty(BaseModel):
    """properties 行（kind check：datatype|object）。"""

    iri: str = Field(min_length=1, max_length=256)
    kind: str = Field(pattern="^(datatype|object)$")
    name: str = Field(min_length=1, max_length=128)
    label: str | None = None
    definition: str | None = None
    domain_iri: str | None = None
    range_iri: str | None = None
    type_of_terms: str | None = None  # ob2:termType（M2 未启用，登记待办）
    functional: bool = False
    constraints: dict[str, Any] = Field(default_factory=dict)  # in/pattern/minCount…（随 shape 投影）
    metadata: dict[str, Any] = Field(default_factory=dict)  # 国标附录 A 10 项（M2 仅 source 留痕）


class ReadModelAxiom(BaseModel):
    """axioms 行（R1 封闭集直接公理登记）。"""

    kind: str = Field(min_length=1, max_length=32)  # subClassOf/disjointWith/…
    subject_iri: str = Field(min_length=1, max_length=256)
    object_iri: str | None = None
    expression: str = Field(min_length=1)  # 可读渲染（OWL 2 函数式语法的 M2 近似，登记待办）
    source: str = Field(default="manual", pattern="^(manual|llm_candidate)$")
    review_state: str | None = None


class ReadModelRule(BaseModel):
    """rules 行（三路由 owl_axiom|shacl|engine；route check 与 uk_rules_version_id_name）。"""

    route: str = Field(pattern="^(owl_axiom|shacl|engine)$")
    name: str = Field(min_length=1, max_length=128)  # R001 式本地编号（版本内唯一）
    description: str | None = None
    event_class_iri: str | None = None  # ECA 事件类（engine 路由）
    condition: str | None = None  # SPARQL ASK / shape 引用
    action_ref: str | None = None  # 行动类 IRI
    severity: str = Field(default="error", pattern="^(error|warn)$")
    enabled: bool = True
    source: str = Field(default="manual", pattern="^(manual|llm_candidate)$")
    review_state: str | None = None


class ReadModelProjection(BaseModel):
    """一个发布版本的完整读模型投影（替换式写：同 ontology+version 先删后插）。"""

    classes: list[ReadModelClass] = Field(default_factory=list)
    properties: list[ReadModelProperty] = Field(default_factory=list)
    axioms: list[ReadModelAxiom] = Field(default_factory=list)
    rules: list[ReadModelRule] = Field(default_factory=list)
