"""L5 本体核心（ontology_core）：TBox 权威管理者（本体核心设计 §1）。

分包：tbox（制品装载/序列化/IRI 校验）/ shacl（引擎 2 门禁封装）/ lint（三路由判定表）/
reasoning（owlrl OWL 2 RL 确定性闭包）/ query（只读 SPARQL 面）/ diff（版本读模型差异）。
编辑期内存图归本层应用态（04 篇 §9 裁决：聚合不持有 rdflib Graph）；零存储/HTTP 依赖。
"""

from .diff import ProjectionDiff, diff_projections
from .lint import LintReport, LintViolation, lint
from .projection import project_tbox
from .query import MAX_QUERY_ROWS, QueryResult, SparqlRejected, execute_readonly, prepare_readonly
from .reasoning import ReasonReport, entail
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
    "MAX_QUERY_ROWS",
    "OB2",
    "TASK",
    "LintReport",
    "LintViolation",
    "ProjectionDiff",
    "QueryResult",
    "ReasonReport",
    "SparqlRejected",
    "ValidationReport",
    "ValidationViolation",
    "default_namespace",
    "diff_projections",
    "entail",
    "execute_readonly",
    "iri_problems",
    "lint",
    "load_turtle",
    "local_name",
    "prepare_readonly",
    "project_tbox",
    "serialize_turtle",
    "validate",
]
