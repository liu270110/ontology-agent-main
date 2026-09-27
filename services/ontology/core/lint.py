"""L5 ontology_core · lint：三路由判定表 + profile 声明校验 + 术语唯一性（本体核心设计 §2.3/§2.4/§4.1）。

路由判定表（§2.3，2026-09-27 lint 化裁决；同一规则只允许一个路由，双源禁止）：
- R1 owl_axiom：公理类型全部落在当前 profile 的允许公理封闭集内（rl=平台默认，集合权威=02 篇 §3.1），
  且表达式结构 RL 合法（equivalentClass 两侧须原子类；DL 公理入 rl/el 直接拒绝）；
- R2 shacl：约束可整体映射为 sh: 算子封闭集（datatype/nodeKind/pattern/in/minCount/maxCount/
  minInclusive/maxInclusive/closed/hasValue，经 sh:property 组合子嵌套）；含集合外构造 → 转 R3 判定；
- R3 engine：①条件为 SPARQL ASK；②绑定事件类；③action_ref 指向行动类；④超 R2 算子集（任一即入）。

规则声明图形态（本批定稿，rules.route 初始值由 lint 判定写入，人工改判越表即违规）：
- R1：规则节点 `ob2:axiomKind "<kind>"` 登记（物化公理三元组随制品）；R2：规则节点兼 sh:NodeShape；
- R3：`ob2:condition "ASK …"^^ob2:sparqlAsk` + `ob2:eventClass` / `ob2:actionRef`。
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SH
from rdflib.term import Literal, Node

from .tbox import OB2, local_name

# ---- R1：rl profile 允许公理封闭集（02 篇 §3.1；含合取/存在量词受限形式与属性链） ----
# rdflib OWL 类未覆盖的公理谓词显式构造（owl2 命名空间不变）
_OWL_ALL_DISJOINT_CLASSES = URIRef(f"{OWL}allDisjointClasses")
_OWL_FUNCTIONAL_PROPERTY = URIRef(f"{OWL}functionalProperty")
_OWL_INVERSE_FUNCTIONAL_PROPERTY = URIRef(f"{OWL}inverseFunctionalProperty")
_OWL_TRANSITIVE_PROPERTY = URIRef(f"{OWL}transitiveProperty")
_OWL_SYMMETRIC_PROPERTY = URIRef(f"{OWL}symmetricProperty")

_R1_CLOSED_PREDICATES: frozenset[str] = frozenset(
    str(u)
    for u in (
        RDFS.subClassOf,
        RDFS.subPropertyOf,
        OWL.equivalentClass,
        OWL.equivalentProperty,
        OWL.disjointWith,
        _OWL_ALL_DISJOINT_CLASSES,
        RDFS.domain,
        RDFS.range,
        _OWL_FUNCTIONAL_PROPERTY,
        _OWL_INVERSE_FUNCTIONAL_PROPERTY,
        _OWL_TRANSITIVE_PROPERTY,
        _OWL_SYMMETRIC_PROPERTY,
        OWL.inverseOf,
        OWL.hasKey,
        OWL.propertyChainAxiom,
        OB2.versionReplacedBy,  # version_replacement 平台扩展 kind（物化为注解公理）
    )
)

# 规则节点 axiomKind 登记名（与上表一一对应；ob2:versionReplacedBy 登记名=version_replacement）
_R1_AXIOM_KINDS: frozenset[str] = frozenset(
    {
        "subClassOf",
        "subPropertyOf",
        "equivalentClass",
        "equivalentProperty",
        "disjointWith",
        "allDisjointClasses",
        "domain",
        "range",
        "functionalProperty",
        "inverseFunctionalProperty",
        "transitiveProperty",
        "symmetricProperty",
        "inverseOf",
        "hasKey",
        "propertyChainAxiom",
        "version_replacement",
    }
)

# DL 公理家族：rl/el profile 禁入（02 篇 §3.1 lint 同款——确定性引擎不可消费）
_DL_AXIOM_PREDICATES: frozenset[str] = frozenset(
    str(u)
    for u in (
        OWL.cardinality,
        OWL.minCardinality,
        OWL.maxCardinality,
        OWL.qualifiedCardinality,
        OWL.someValuesFrom,
        OWL.allValuesFrom,
        OWL.unionOf,
        OWL.intersectionOf,
        OWL.oneOf,
        OWL.complementOf,
        OWL.disjointUnionOf,
    )
)

# ---- R2：SHACL 算子封闭集（§2.3 R2 行；sh:property 为组合子，sh:path 为属性 shape 结构键） ----
_SH_IN = URIRef(f"{SH}in")  # sh:in（rdflib SH 类未定义该属性名，显式构造）
_R2_OPERATORS: frozenset[str] = frozenset(
    str(u)
    for u in (
        SH.datatype,
        SH.nodeKind,
        SH.pattern,
        _SH_IN,
        SH.minCount,
        SH.maxCount,
        SH.minInclusive,
        SH.maxInclusive,
        SH.closed,
        SH.hasValue,
    )
)
_R2_SHAPE_STRUCTURAL: frozenset[str] = frozenset(str(u) for u in (SH.path, SH.property))
_R2_TARGETS: frozenset[str] = frozenset(str(u) for u in (SH.targetClass, SH.targetSubjectsOf, SH.targetObjectsOf))
_SHAPE_FORM_MARKERS: tuple[URIRef, ...] = (RDF.type, SH.targetClass, SH.targetSubjectsOf, SH.targetObjectsOf)

PROFILES: frozenset[str] = frozenset({"rl", "el", "dl"})
DEFAULT_PROFILE = "rl"


class LintViolation(BaseModel):
    """lint 违规（subject=规则/术语 IRI 原文指针，结果可追溯）。"""

    code: str
    message: str
    subject: str | None = None


class LintReport(BaseModel):
    """lint 报告：ok=无违规；routes=规则 IRI → 判定路由（rules.route 初始值来源，§2.3）。"""

    ok: bool
    violations: list[LintViolation] = []
    routes: dict[str, str] = Field(default_factory=dict)
    profile: str = DEFAULT_PROFILE


def read_profile(graph: Graph) -> tuple[str, LintViolation | None]:
    """读 profile 声明（owl:Ontology 头 `ob2:profile "rl|el|dl"`；缺省=rl 平台默认）。"""
    declared: list[str] = []
    for subject in graph.subjects(RDF.type, OWL.Ontology):
        for declared_value in graph.objects(subject, OB2.profile):
            declared.append(str(declared_value))
    unique = set(declared)
    if len(unique) > 1:
        return DEFAULT_PROFILE, LintViolation(
            code="LINT_PROFILE_CONFLICT", message=f"profile 声明冲突: {sorted(unique)}"
        )
    if unique and next(iter(unique)) not in PROFILES:
        value = next(iter(unique))
        return DEFAULT_PROFILE, LintViolation(
            code="LINT_PROFILE_INVALID", message=f"profile 必须为 {sorted(PROFILES)} 之一，得 {value!r}", subject=value
        )
    return (next(iter(unique)) if unique else DEFAULT_PROFILE), None


def lint(graph: Graph, profile: str | None = None) -> LintReport:
    """编辑期 lint（提交预检第一步，§6.1）：profile+判定表定路由；越表改判/双源/DL 公理直接拒绝。"""
    violations: list[LintViolation] = []
    resolved, profile_violation = read_profile(graph)
    if profile is not None and profile not in PROFILES:
        violations.append(
            LintViolation(code="LINT_PROFILE_INVALID", message=f"未知 profile {profile!r}（合法={sorted(PROFILES)}）")
        )
        profile = None
    if profile_violation is not None:
        violations.append(profile_violation)
    effective_profile = profile or resolved

    violations.extend(_lint_direct_axioms(graph, effective_profile))
    routes, rule_violations = _route_rules(graph, effective_profile)
    violations.extend(rule_violations)
    violations.extend(_lint_action_closure(graph))
    violations.extend(_lint_term_uniqueness(graph))
    return LintReport(ok=not violations, violations=violations, routes=routes, profile=effective_profile)


def _lint_direct_axioms(graph: Graph, profile: str) -> list[LintViolation]:
    """直接公理扫描（R1 制品形态）：DL 公理入 rl/el 拒绝；equivalentClass 两侧须原子类（RL 合法性）。"""
    violations: list[LintViolation] = []
    for subject, predicate, obj in graph:
        predicate_iri = str(predicate)
        if predicate_iri in _DL_AXIOM_PREDICATES and profile != "dl":
            violations.append(
                LintViolation(
                    code="LINT_AXIOM_NOT_RL",
                    message=f"DL 公理 {local_name(predicate_iri)} 不可入 {profile} 档本体（02 篇 §3.1）",
                    subject=str(subject),
                )
            )
        elif predicate_iri == str(OWL.equivalentClass) and not isinstance(obj, URIRef):
            violations.append(
                LintViolation(
                    code="LINT_EXPR_NOT_RL",
                    message="equivalentClass 两侧须原子类——带限制表达式不入 R1，走 SHACL/定义类通道（§2.3）",
                    subject=str(subject),
                )
            )
    return violations


def _route_rules(graph: Graph, effective_profile: str) -> tuple[dict[str, str], list[LintViolation]]:
    """规则节点三路由判定（§2.3 判定表，按 R1→R2→R3 顺序、命中即停；双源/无路由即违规）。"""
    routes: dict[str, str] = {}
    violations: list[LintViolation] = []
    for rule in graph.subjects(RDF.type, OB2.Rule):
        rule_iri = str(rule)
        hit_r1, r1_violation = _r1_evidence(graph, rule, effective_profile)
        if r1_violation is not None:
            violations.append(r1_violation)
        hit_r2, beyond_r2 = _r2_evidence(graph, rule)
        hit_r3 = _r3_evidence(graph, rule) or beyond_r2  # 超 R2 算子集 → 转 R3 判定（判定表 ④）
        hits = [name for name, hit in (("owl_axiom", hit_r1), ("shacl", hit_r2), ("engine", hit_r3)) if hit]
        if len(hits) > 1:
            violations.append(
                LintViolation(
                    code="LINT_ROUTE_DUAL", message=f"同一规则命中多路由 {hits}——双源禁止（§2.3）", subject=rule_iri
                )
            )
        elif not hits:
            violations.append(
                LintViolation(
                    code="LINT_ROUTE_UNKNOWN",
                    message="规则无任何路由判定证据（须 axiomKind/shape/ASK·事件·行动绑定）",
                    subject=rule_iri,
                )
            )
        else:
            routes[rule_iri] = hits[0]
    return routes, violations


def _r1_evidence(graph: Graph, rule: Node, profile: str) -> tuple[bool, LintViolation | None]:
    """R1 判定：公理类型∈profile 允许封闭集；越集（DL/越表改判）→ 判 R1 未命中且违规（不进发布预检）。"""
    kinds = [str(value) for value in graph.objects(rule, OB2.axiomKind)]
    if not kinds:
        return False, None
    for kind in kinds:
        if kind not in _R1_AXIOM_KINDS and profile != "dl":
            return False, LintViolation(
                code="LINT_AXIOM_NOT_RL",
                message=f"公理类型 {kind!r} 不在 {profile} 允许公理封闭集内——人工改判越表，编辑期拒绝（§2.3）",
                subject=str(rule),
            )
    return True, None


def _r2_evidence(graph: Graph, rule: Node) -> tuple[bool, bool]:
    """R2 判定：规则具 shape 形态且算子全部在封闭集内；含集合外构造 → (False, beyond=True)。"""
    shape_present = any(
        (predicate == RDF.type and SH.NodeShape in graph.objects(rule, RDF.type)) or str(predicate) in _R2_TARGETS
        for predicate in graph.predicates(rule, None)
    )
    if not shape_present:
        return False, False
    beyond = False
    operator_allow = _R2_OPERATORS | _R2_SHAPE_STRUCTURAL | _R2_TARGETS
    for predicate in graph.predicates(rule, None):
        predicate_iri = str(predicate)
        if predicate_iri.startswith(str(SH)) and predicate_iri not in operator_allow:
            beyond = True  # sh:sparql / sh:not / sh:and 等集合外构造（复杂 shape 逻辑 → 转 R3 判定）
        elif predicate_iri == str(SH.property):
            for prop_shape in graph.objects(rule, SH.property):
                for inner in graph.predicates(prop_shape, None):
                    inner_iri = str(inner)
                    if inner_iri.startswith(str(SH)) and inner_iri not in _R2_OPERATORS | _R2_SHAPE_STRUCTURAL:
                        beyond = True
    return not beyond, beyond


def _r3_evidence(graph: Graph, rule: Node) -> bool:
    """R3 判定 ①②③：SPARQL ASK 条件 / 事件类绑定 / 行动类绑定（任一即入）。"""
    for condition in graph.objects(rule, OB2.condition):
        # isinstance 收窄（Node 无 datatype 属性；ASK 条件字面量携带 ob2:sparqlAsk 类型）
        if isinstance(condition, Literal) and condition.datatype is not None:
            if str(condition.datatype) == str(OB2.sparqlAsk):
                return True
        if "ASK" in str(condition).upper():
            return True
    return graph.value(rule, OB2.eventClass) is not None or graph.value(rule, OB2.actionRef) is not None


def _lint_action_closure(graph: Graph) -> list[LintViolation]:
    """行动类闭环（§2.4）：每个行动类必须反向引用触发事件与守卫规则，否则闭环在本体层不可追溯。"""
    violations: list[LintViolation] = []
    for action in graph.subjects(RDFS.subClassOf, OB2.Action):
        missing = [
            name
            for name, predicate in (("triggeredByEvent", OB2.triggeredByEvent), ("guardedByRule", OB2.guardedByRule))
            if graph.value(action, predicate) is None
        ]
        if missing:
            violations.append(
                LintViolation(
                    code="LINT_ACTION_NOT_CLOSED",
                    message=f"行动类缺少 {'/'.join(missing)} 反向引用——逆向闭环断裂（§2.4）",
                    subject=str(action),
                )
            )
    return violations


def _lint_term_uniqueness(graph: Graph) -> list[LintViolation]:
    """术语唯一性（国标底线 + §2.3）：同名多 IRI 检测，按命名空间分域判定——
    一个概念只有一个规范名（近义词登记为别名）；跨命名空间同名=平台级隔离设计（§2：
    task:/ob2: 与业务本体「分命名空间隔离、互不污染」），不构成违规。
    """
    by_name: dict[tuple[str, str], set[str]] = {}
    term_types = (OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty)
    for term_type in term_types:
        for term in graph.subjects(RDF.type, term_type):
            iri = str(term)
            by_name.setdefault((_namespace_of(iri), local_name(iri)), set()).add(iri)
    return [
        LintViolation(
            code="LINT_TERM_DUP",
            message=f"同名多 IRI：{ns}#{name} → {sorted(iris)}（术语唯一性，近义词应登记为别名）",
            subject=f"{ns}#{name}",
        )
        for (ns, name), iris in sorted(by_name.items())
        if len(iris) > 1
    ]


def _namespace_of(iri: str) -> str:
    return iri.rsplit("#", 1)[0] if "#" in iri else iri.rsplit("/", 1)[0]
