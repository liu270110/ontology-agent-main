"""L5 本体核心（ontology_core）：TBox 权威管理者（本体核心设计 §1）。

分包：tbox（制品装载/序列化/IRI 校验）/ shacl（引擎 2 门禁封装）/ lint（三路由判定表）。
编辑期内存图归本层应用态（04 篇 §9 裁决：聚合不持有 rdflib Graph）；零存储/HTTP 依赖。
"""

from .lint import LintReport, LintViolation, lint
from .projection import project_tbox
from .shacl import ValidationReport, ValidationViolation, validate
from .tbox import (
    OB2,
    TASK,
    default_namespace,
    iri_problems,
    load_turtle,
    local_name,
    serialize_turtle,
)

__all__ = [
    "OB2",
    "TASK",
    "LintReport",
    "LintViolation",
    "ValidationReport",
    "ValidationViolation",
    "default_namespace",
    "iri_problems",
    "lint",
    "load_turtle",
    "local_name",
    "project_tbox",
    "serialize_turtle",
    "validate",
]
