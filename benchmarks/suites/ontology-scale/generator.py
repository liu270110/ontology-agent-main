"""ontology-scale 合成本体梯度生成器（docs/Agent/16 §1 G 域；问题源=红队审查 G1/F3）。

确定性合成（种子固定，同种子同输出）：电力停电域词汇合成三档规模梯度 10²/10³/10⁴——
类数=档位 N（设备类型/工单类型/故障模式族三族按 1:1:1 分摊），属性形状数=2N（每类 2 个
PropertyShape，家族属性池共享 IRI），实例数=10N（instance_multiplier 可调）。SHACL shapes
随本体同梯度生成（每类一个 NodeShape，R2 路由产物的同构形态），并注入少量故意违例实例
（violation_ratio，每违例实例恰好 1 条违例，供校验命中数可解析核对——shapes 有效性=
违例形态非 conforms 且命中数=期望数）。

确定性纪律：一切取值要么由索引派生（约束参数/命名/sh:in 列表节点 IRI），要么出自
random.Random(seed)——禁 time/uuid/BNode/环境读取；同种子双次生成序列化逐字节一致
（tests/benchmarks 锁）。

F3 变异面（reindex_consistency 探针供体）：三种形状契约变异（range_tighten/enum_narrow/
required_add，均保证可检出）+ 一个 TBox-only 对照变异（rdfs:range 改动、shapes 不动——
inference=none 下 pySHACL 不读 rdfs:range，预期不可检出，诚实记录门禁盲区）。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SH, XSD

# 命名空间（tbox.py default_namespace 同构：http://ontology-agent.local/o/{tenant}/{slug}#）
ONTO_NS = "http://ontology-agent.local/o/bench/onto-scale#"
NS = Namespace(ONTO_NS)
OB2 = Namespace("https://ontology-agent.dev/ns/ob2#")  # 平台顶类（本体核心设计 §4.1）
PW_PREFIX = "pw"

# 规模梯度三档（16 篇 §2：validate_latency@10^2/10^3/10^4）
TIERS: tuple[int, ...] = (100, 1000, 10000)

# 电力停电域词汇（类命名池，索引轮转；IRI 纯 ASCII——tbox.py iri_problems 禁中文，中文进 rdfs:label）
_FAMILY_VOCAB: dict[str, tuple[tuple[str, str], ...]] = {
    # 家族根 → ((英文词, 中文词), ...)：设备类型/工单类型/故障模式族
    "Equipment": (
        ("Transformer", "配电变压器"),
        ("Breaker", "断路器"),
        ("Disconnector", "隔离开关"),
        ("FeederLine", "馈线"),
        ("DistributionCabinet", "配电柜"),
        ("CapacitorBank", "电容器组"),
        ("VoltageTransformer", "电压互感器"),
        ("CurrentTransformer", "电流互感器"),
        ("SurgeArrester", "避雷器"),
        ("PoleMountedSwitch", "柱上开关"),
    ),
    "WorkOrder": (
        ("OutageWorkOrder", "停电工单"),
        ("MaintenanceWorkOrder", "检修工单"),
        ("InspectionWorkOrder", "巡检工单"),
        ("EmergencyRepairOrder", "抢修工单"),
        ("AcceptanceWorkOrder", "验收工单"),
    ),
    "FaultMode": (
        ("InsulationFailure", "绝缘故障"),
        ("OverloadTrip", "过载跳闸"),
        ("LightningFlashover", "雷击闪络"),
        ("MechanicalJam", "机械卡涩"),
        ("JointOverheating", "接头发热"),
        ("SecondaryCircuitFailure", "二次回路失效"),
        ("ProtectionMaloperation", "保护误动"),
        ("AnimalCausedFault", "小动物故障"),
    ),
}
_ROOT_LABEL = {"Equipment": "设备类型", "WorkOrder": "工单类型", "FaultMode": "故障模式族"}

_REGION_ENUM = ("RGN-001", "RGN-002", "RGN-003")  # 位置枚举（regionCode sh:in）
_REGION_ILLEGAL = "RGN-999"  # 故意违例枚举外值
_FEEDER_PATTERN = "FDR-[0-9]{3}"  # feederId sh:pattern
_TIME_BASE = datetime(2026, 9, 1, 0, 0, 0)  # 时间属性取值基点（确定性，禁 now()）

# F3 形状契约变异三型（均可保证检出）+ TBox-only 对照型（inference=none 下预期盲区）
MUTATION_KINDS: tuple[str, ...] = ("range_tighten", "enum_narrow", "required_add")
TBOX_ONLY_KIND = "tbox_range_only"


@dataclass(frozen=True)
class PropSpec:
    """单类单属性的形状约束镜像（摘要构建/F3 变异定位的纯数据面）。"""

    local: str  # 属性 IRI 本地名（家族属性池共享）
    kind: str  # double_range | datetime | string_enum | string_pattern
    params: tuple[tuple[str, float | str], ...] = field(default_factory=tuple)  # 约束参数（low/high/enum/pattern）


@dataclass(frozen=True)
class SyntheticOntology:
    """一档合成产物：图对象（校验直喂）+ 组装单元文本（token 成本面）+ 解析可核对的计数。"""

    scale: int
    seed: int
    instance_multiplier: int
    tbox: Graph
    shapes: Graph
    abox_clean: Graph
    abox_violations: Graph
    violations_expected: int
    class_props: dict[str, tuple[PropSpec, ...]]  # 类 local name → 形状约束镜像（F3 变异/摘要供体）
    summary_units: tuple[str, ...]  # TBox 摘要块组装单元（类名+属性清单，每类一条）
    full_units: tuple[str, ...]  # 全量 schema 组装单元（类公理+形状 Turtle，与摘要单元同类对齐）
    prolog: str  # 前缀声明头（两模式共享，计入组装成本）
    tbox_turtle: str
    shapes_turtle: str
    counts: dict[str, int]

    def full_schema_text(self) -> str:
        """全量 schema 注入文本（prolog + 全部类公理/形状块）——prolog+units 可独立解析为合法 Turtle。"""
        return self.prolog + "\n".join(self.full_units)

    def summary_text(self) -> str:
        """TBox 摘要注入文本（prolog + 类名+属性清单行）——grounding tier=1 稳定知识块的合成形态
        （services/agent/business/kernel/compaction.py:32 口径）。"""
        return self.prolog + "\n".join(self.summary_units)


# ---------------------------------------------------------------------------
# 生成（确定性）
# ---------------------------------------------------------------------------


def generate(
    scale: int,
    *,
    seed: int = 20261007,
    instance_multiplier: int = 10,
    violation_ratio: float = 0.01,
) -> SyntheticOntology:
    """合成一档本体：N 类 × 2N 属性形状 × 10N 实例（比例可调），shapes 同梯度 + 故意违例。

    scale 须 ≥3（三族分摊 base+余数逐族加一，类数精确=N）；violation_ratio ∈ [0,1)。
    返回对象含四张图：TBox / shapes / 干净 ABox / 带违例 ABox（违例数=解析可核对）。
    """
    if scale < 3:
        raise ValueError(f"scale 须 ≥3（三族每族至少 1 类），收到 {scale}")
    if not 0 <= violation_ratio < 1:
        raise ValueError(f"violation_ratio 须 ∈ [0,1)，收到 {violation_ratio}")
    rng = random.Random(seed)
    # 三族分摊（base+余数逐族加一）：档位类数精确等于 scale（100/1000/10000 不要求整除 3）
    family_sizes = _family_quotas(scale, len(_FAMILY_VOCAB))
    n_instances = scale * instance_multiplier
    n_violate = round(n_instances * violation_ratio)

    tbox = Graph()
    shapes = Graph()
    abox_clean = Graph()
    abox_violations = Graph()
    for g in (tbox, shapes, abox_clean, abox_violations):
        _bind_prefixes(g)

    summary_units: list[str] = []
    full_units: list[str] = []
    class_props: dict[str, tuple[PropSpec, ...]] = {}
    n_props = 0
    instance_seq = 0
    # 违例槽位按族均衡（base+余数逐族加一）——保证小比例下三族违例形态都会出现
    violation_quota = _family_quotas(n_violate, len(_FAMILY_VOCAB))

    # 三族根类（rdfs:subClassOf ob2:Object，对齐平台 TBox 顶类惯例）
    for family in _FAMILY_VOCAB:
        root = NS[family]
        tbox.add((root, RDF.type, OWL.Class))
        tbox.add((root, RDFS.subClassOf, OB2.Object))
        tbox.add((root, RDFS.label, Literal(_ROOT_LABEL[family], lang="zh")))

    for fam_idx, (family, vocab) in enumerate(_FAMILY_VOCAB.items()):
        root = NS[family]
        family_violations = violation_quota[fam_idx]  # 本族前 quota 个类实例违例（族内确定性）
        for i in range(family_sizes[fam_idx]):
            word_en, word_zh = vocab[i % len(vocab)]
            cls_local = f"{word_en}{i:05d}"
            cls = NS[cls_local]
            label = f"{word_zh}-{i:05d}"
            tbox.add((cls, RDF.type, OWL.Class))
            tbox.add((cls, RDFS.subClassOf, root))
            tbox.add((cls, RDFS.label, Literal(label, lang="zh")))

            # 家族属性池：每类 2 个属性形状（属性 IRI 家族共享，约束参数索引派生——确定性）
            specs = _prop_specs(family, i)
            n_props += len(specs)
            shape_iri = NS[f"Shape_{cls_local}"]
            prop_shape_iris: list[str] = []
            for spec in specs:
                ps_local = f"Shape_{cls_local}_{spec.local}"
                ps_iri = NS[ps_local]
                prop_shape_iris.append(ps_local)
                shapes.add((shape_iri, RDF.type, SH.NodeShape))
                shapes.add((shape_iri, SH.targetClass, cls))
                shapes.add((shape_iri, SH.property, ps_iri))
                shapes.add((ps_iri, RDF.type, SH.PropertyShape))
                shapes.add((ps_iri, SH.path, NS[spec.local]))
                shapes.add((ps_iri, SH.minCount, Literal(1)))
                _add_selector_shapes(shapes, ps_iri, cls_local, spec)
                # 属性声明进 TBox（rdfs:domain/range，全量 schema 与 TBox-only 对照变异的组成）
                prop_iri = NS[spec.local]
                tbox.add((prop_iri, RDF.type, RDF.Property))
                tbox.add((prop_iri, RDFS.domain, cls))
                tbox.add((prop_iri, RDFS.range, _range_of(spec)))
            class_props[cls_local] = specs

            summary_units.append(_summary_unit(cls_local, label, family, specs))
            full_units.append(_full_unit(cls_local, label, prop_shape_iris, specs))

            # 实例：10N 按类均分（每类 instance_multiplier 个），两形态同源对照——
            # 干净形态=全量总体的合规形态；违例形态=同总体按族配额注入违例（配额实例
            # 不从干净形态剔除：F3 变异靶须在旧实例总体上可检出，剔会造成靶类空洞）
            for k in range(instance_multiplier):
                inst_local = f"inst_{cls_local}_{k:05d}"
                values = _instance_values(rng, specs)
                _add_instance(abox_clean, cls, inst_local, specs, values, violating=False)
                if family_violations > 0:
                    family_violations -= 1
                    _add_instance(abox_violations, cls, inst_local, specs, values, violating=True)
                else:
                    _add_instance(abox_violations, cls, inst_local, specs, values, violating=False)
                instance_seq += 1

    counts = {
        "classes": sum(family_sizes),
        "properties": n_props,
        "instances_per_form": instance_seq,
        "violations_expected": n_violate,
        "tbox_triples": len(tbox),
        "shapes_triples": len(shapes),
        "abox_clean_triples": len(abox_clean),
        "abox_violations_triples": len(abox_violations),
    }
    return SyntheticOntology(
        scale=scale,
        seed=seed,
        instance_multiplier=instance_multiplier,
        tbox=tbox,
        shapes=shapes,
        abox_clean=abox_clean,
        abox_violations=abox_violations,
        violations_expected=n_violate,
        class_props=class_props,
        summary_units=tuple(summary_units),
        full_units=tuple(full_units),
        prolog=_prolog(),
        tbox_turtle=tbox.serialize(format="turtle"),
        shapes_turtle=shapes.serialize(format="turtle"),
        counts=counts,
    )


def _bind_prefixes(g: Graph) -> None:
    g.bind(PW_PREFIX, NS)
    g.bind("ob2", OB2)
    g.bind("owl", OWL)
    g.bind("rdfs", RDFS)
    g.bind("sh", SH)
    g.bind("xsd", XSD)


def _prolog() -> str:
    return (
        f"@prefix {PW_PREFIX}: <{ONTO_NS}> .\n"
        f"@prefix ob2: <{str(OB2)}> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
    )


def _family_quotas(n_violate: int, n_families: int) -> list[int]:
    """违例配额按族分摊（base+余数逐族加一；总和恰等于 n_violate，命中数解析核对不漂移）。"""
    base, rem = divmod(n_violate, n_families)
    return [base + (1 if f < rem else 0) for f in range(n_families)]


def _prop_specs(family: str, i: int) -> tuple[PropSpec, ...]:
    """家族属性池 → 该类的 2 个属性约束（参数由索引派生：确定性、且类间有差异）。"""
    if family == "Equipment":
        v_high = round(10.0 + (i % 50) * 0.7, 1)
        a_high = round(50.0 + (i % 30) * 1.5, 1)
        return (
            PropSpec("ratedVoltageKv", "double_range", (("low", 3.0), ("high", v_high))),
            PropSpec("ratedCurrentA", "double_range", (("low", 5.0), ("high", a_high))),
        )
    if family == "WorkOrder":
        return (PropSpec("reportedAt", "datetime"), PropSpec("restoredAt", "datetime"))
    if family == "FaultMode":
        return (
            PropSpec("regionCode", "string_enum", (("enum", "\x1f".join(_REGION_ENUM)),)),
            PropSpec("feederId", "string_pattern", (("pattern", _FEEDER_PATTERN),)),
        )
    raise ValueError(f"未知家族: {family}")  # pragma: no cover —— 家族词表封闭，防御性


def _range_of(spec: PropSpec) -> URIRef:
    """属性 rdfs:range（TBox 声明面；TBox-only 对照变异的改写对象）。"""
    return {
        "double_range": XSD.double,
        "datetime": XSD.dateTime,
        "string_enum": XSD.string,
        "string_pattern": XSD.string,
    }[spec.kind]


def _add_selector_shapes(shapes: Graph, ps_iri: URIRef, cls_local: str, spec: PropSpec) -> None:
    """选择性约束（R2 算子封闭集子集：datatype/in/pattern/minInclusive/maxInclusive）。

    sh:in 值=RDF Collection（projection.py:176 同构读取面）；列表节点用确定性 URIRef
    （禁 BNode——uuid 派生 id 破坏跨进程序列化确定性）。
    """
    if spec.kind == "double_range":
        params = dict(spec.params)
        shapes.add((ps_iri, SH.datatype, XSD.double))
        shapes.add((ps_iri, SH.minInclusive, Literal(float(params["low"]))))
        shapes.add((ps_iri, SH.maxInclusive, Literal(float(params["high"]))))
    elif spec.kind == "datetime":
        shapes.add((ps_iri, SH.datatype, XSD.dateTime))
    elif spec.kind == "string_enum":
        shapes.add((ps_iri, SH.datatype, XSD.string))
        _add_in_list(shapes, ps_iri, cls_local, spec.local, list(_REGION_ENUM))
    elif spec.kind == "string_pattern":
        shapes.add((ps_iri, SH.datatype, XSD.string))
        shapes.add((ps_iri, SH.pattern, Literal(str(dict(spec.params)["pattern"]))))
    else:
        raise ValueError(f"未知属性约束类型: {spec.kind}")  # pragma: no cover


def _add_in_list(shapes: Graph, ps_iri: URIRef, cls_local: str, prop_local: str, values: list[str]) -> None:
    """sh:in → RDF Collection（确定性 URIRef 列表节点；projection.py Collection 读取同构）。"""
    head = NS[f"inlist_{cls_local}_{prop_local}_0"]
    shapes.add((ps_iri, SH["in"], head))  # SH.in 属性不可点访问（`in` 为 Python 关键字）
    for i, value in enumerate(values):
        node = NS[f"inlist_{cls_local}_{prop_local}_{i}"]
        shapes.add((node, RDF.first, Literal(value)))
        rest = NS[f"inlist_{cls_local}_{prop_local}_{i + 1}"] if i + 1 < len(values) else RDF.nil
        shapes.add((node, RDF.rest, rest))


def _remove_in_list(shapes: Graph, ps_iri: URIRef) -> None:
    """摘除 sh:in 及其 Collection 链（enum_narrow 变异用；残留链无害但清干净防歧义）。"""
    head = shapes.value(ps_iri, SH["in"])
    if head is None:
        return
    shapes.remove((ps_iri, SH["in"], head))
    while isinstance(head, URIRef) and head != RDF.nil:
        first = shapes.value(head, RDF.first)
        rest = shapes.value(head, RDF.rest)
        shapes.remove((head, RDF.first, first))
        shapes.remove((head, RDF.rest, rest))
        head = rest


def _instance_values(rng: random.Random, specs: tuple[PropSpec, ...]) -> dict[str, float | str]:
    """单实例属性值（rng 驱动、全部落在约束内；返回 local→值）。"""
    values: dict[str, float | str] = {}
    for spec in specs:
        if spec.kind == "double_range":
            params = dict(spec.params)
            values[spec.local] = round(rng.uniform(float(params["low"]), float(params["high"])), 3)
        elif spec.kind == "datetime":
            offset_min = rng.randrange(0, 30 * 24 * 60)
            values[spec.local] = (_TIME_BASE + timedelta(minutes=offset_min)).strftime("%Y-%m-%dT%H:%M:%S")
        elif spec.kind == "string_enum":
            values[spec.local] = rng.choice(_REGION_ENUM)
        elif spec.kind == "string_pattern":
            values[spec.local] = f"FDR-{rng.randrange(1000):03d}"
        else:
            raise ValueError(f"未知属性约束类型: {spec.kind}")  # pragma: no cover
    return values


def _add_instance(
    graph: Graph,
    cls: URIRef,
    inst_local: str,
    specs: tuple[PropSpec, ...],
    values: dict[str, float | str],
    *,
    violating: bool,
) -> None:
    """实例入图（最具体类型标注——shacl.py 门禁语义：子类展开靠 TBox subClassOf 并入）。

    violating=True 时按家族注入恰好 1 条违例（数值超界/缺必填/枚举外——命中数可解析核对）。
    """
    inst = NS[inst_local]
    graph.add((inst, RDF.type, cls))
    family = _family_of(specs)
    for idx, spec in enumerate(specs):
        if violating and family == "Equipment" and idx == 0:  # 电气参数超上界（sh:maxInclusive 命中）
            params = dict(spec.params)
            low, high = float(params["low"]), float(params["high"])
            graph.add((inst, NS[spec.local], Literal(high + (high - low) * 0.1 + 1.0)))
            continue
        if violating and family == "WorkOrder" and idx == 1:  # 缺必填 restoredAt（sh:minCount 命中）
            continue
        if violating and family == "FaultMode" and spec.kind == "string_enum":  # 枚举外（sh:in 命中）
            graph.add((inst, NS[spec.local], Literal(_REGION_ILLEGAL)))
            continue
        value = values[spec.local]
        if isinstance(value, float):
            graph.add((inst, NS[spec.local], Literal(value)))
        elif spec.kind == "datetime":
            graph.add((inst, NS[spec.local], Literal(str(value), datatype=XSD.dateTime)))
        else:
            graph.add((inst, NS[spec.local], Literal(str(value))))


def _family_of(specs: tuple[PropSpec, ...]) -> str:
    first = specs[0].local
    if first.startswith("rated"):
        return "Equipment"
    if first == "reportedAt":
        return "WorkOrder"
    return "FaultMode"


# ---------------------------------------------------------------------------
# 组装单元文本（assemble_token_cost 两模式的供体；与图三元组同源同环生成）
# ---------------------------------------------------------------------------


def _summary_unit(cls_local: str, label: str, family: str, specs: tuple[PropSpec, ...]) -> str:
    """TBox 摘要块单元（类名+属性清单，一行一类）：grounding tier=1 稳定知识的合成形态。"""
    parts = []
    for spec in specs:
        if spec.kind == "double_range":
            params = dict(spec.params)
            parts.append(f"{spec.local}:double[{params['low']},{params['high']}]min1")
        elif spec.kind == "datetime":
            parts.append(f"{spec.local}:dateTime min1")
        elif spec.kind == "string_enum":
            params = dict(spec.params)
            parts.append(f"{spec.local}:string in({str(params['enum']).replace(chr(31), '|')})min1")
        else:
            params = dict(spec.params)
            parts.append(f"{spec.local}:string pattern({params['pattern']})min1")
    return f"{PW_PREFIX}:{cls_local}｜{label}｜{_ROOT_LABEL[family]}｜属性: " + "; ".join(parts)


def _full_unit(cls_local: str, label: str, prop_shape_iris: list[str], specs: tuple[PropSpec, ...]) -> str:
    """全量 schema 单元（类公理 + NodeShape + 全部 PropertyShape 的 Turtle 块）。

    与 prolog 拼接后可独立解析（tests 锁：full_schema_text 逐字节可 parse）。
    """
    lines = [
        f"{PW_PREFIX}:{cls_local} a owl:Class ; rdfs:subClassOf {PW_PREFIX}:{_family_of(specs)} ; "
        f'rdfs:label "{label}"@zh .',
        f"{PW_PREFIX}:Shape_{cls_local} a sh:NodeShape ; sh:targetClass {PW_PREFIX}:{cls_local} ; sh:property "
        + " , ".join(f"{PW_PREFIX}:{p}" for p in prop_shape_iris)
        + " .",
    ]
    for spec, ps_local in zip(specs, prop_shape_iris, strict=True):
        lines.append(_prop_shape_turtle(ps_local, spec))
    return "\n".join(lines)


def _prop_shape_turtle(ps_local: str, spec: PropSpec) -> str:
    base = f"{PW_PREFIX}:{ps_local} a sh:PropertyShape ; sh:path {PW_PREFIX}:{spec.local} ; sh:minCount 1"
    if spec.kind == "double_range":
        params = dict(spec.params)
        return (
            f"{base} ; sh:datatype xsd:double ; sh:minInclusive {float(params['low'])} ; "
            f"sh:maxInclusive {float(params['high'])} ."
        )
    if spec.kind == "datetime":
        return f"{base} ; sh:datatype xsd:dateTime ."
    if spec.kind == "string_enum":
        params = dict(spec.params)
        enum_items = " ".join(f'"{v}"' for v in str(params["enum"]).split("\x1f"))  # Turtle Collection 空格分隔
        return f"{base} ; sh:datatype xsd:string ; sh:in ( {enum_items} ) ."
    params = dict(spec.params)
    return f"{base} ; sh:datatype xsd:string ; sh:pattern \"{params['pattern']}\" ."


# ---------------------------------------------------------------------------
# F3 变异面（reindex_consistency 探针供体；对 shapes/TBox 图的确定性改写）
# ---------------------------------------------------------------------------


def mutation_targets(onto: SyntheticOntology) -> list[dict[str, str]]:
    """变异靶清单（确定性：三契约型各按家族取排序首类 + TBox-only 对照随 range_tighten 靶）。"""
    equipment = sorted(c for c, specs in onto.class_props.items() if specs[0].kind == "double_range")
    workorder = sorted(c for c, specs in onto.class_props.items() if specs[0].local == "reportedAt")
    faultmode = sorted(c for c, specs in onto.class_props.items() if specs[0].kind == "string_enum")
    if not (equipment and workorder and faultmode):
        raise ValueError("三家族类清单存在空族，无法布变异靶")
    return [
        {"kind": "range_tighten", "class_local": equipment[0]},
        {"kind": "enum_narrow", "class_local": faultmode[0]},
        {"kind": "required_add", "class_local": workorder[0]},
        {"kind": TBOX_ONLY_KIND, "class_local": equipment[0]},
    ]


def apply_mutation(onto: SyntheticOntology, target: dict[str, str]) -> tuple[Graph, Graph, str]:
    """对 (shapes, tbox) 施加一处变异，返回 (变异后 shapes, 变异后 tbox, 变异描述)。

    契约三型保证可检出（变异直接命中该类全部实例的合规边界）；TBox-only 型只改
    rdfs:range 不动 shapes——inference=none 下 pySHACL 不读 rdfs:range，预期检不出
    （平台盲区诚实记录：R2 路由重生成 shapes 才能覆盖，见 metrics.reindex_consistency）。
    """
    kind, cls_local = target["kind"], target["class_local"]
    specs = onto.class_props[cls_local]
    shapes = Graph()
    for triple in onto.shapes:
        shapes.add(triple)
    tbox = Graph()
    for triple in onto.tbox:
        tbox.add(triple)
    _bind_prefixes(shapes)
    _bind_prefixes(tbox)

    if kind == "range_tighten":
        spec = specs[0]
        ps_iri = NS[f"Shape_{cls_local}_{spec.local}"]
        low = float(dict(spec.params)["low"])
        shapes.remove((ps_iri, SH.maxInclusive, None))
        shapes.add((ps_iri, SH.maxInclusive, Literal(low)))  # 上界压到下界：该类全部实例超界
        return shapes, tbox, f"{cls_local}.{spec.local} maxInclusive 收紧至 {low}"
    if kind == "enum_narrow":
        spec = next(s for s in specs if s.kind == "string_enum")
        ps_iri = NS[f"Shape_{cls_local}_{spec.local}"]
        _remove_in_list(shapes, ps_iri)
        _add_in_list(shapes, ps_iri, cls_local, spec.local, ["RGN-000"])  # 收窄到域外值：全部实例超界
        return shapes, tbox, f"{cls_local}.{spec.local} sh:in 收窄至 [RGN-000]"
    if kind == "required_add":
        spec = specs[1]
        ps_iri = NS[f"Shape_{cls_local}_{spec.local}"]
        shapes.remove((ps_iri, SH.minCount, None))
        shapes.add((ps_iri, SH.minCount, Literal(2)))  # 必填翻倍：单值实例全部缺失
        return shapes, tbox, f"{cls_local}.{spec.local} minCount 1→2"
    if kind == TBOX_ONLY_KIND:
        spec = specs[0]
        tbox.remove((NS[spec.local], RDFS.range, None))
        tbox.add((NS[spec.local], RDFS.range, XSD.string))  # TBox rdfs:range 改动、shapes 不动
        return shapes, tbox, f"{cls_local}.{spec.local} rdfs:range→xsd:string（仅 TBox）"
    raise ValueError(f"未知变异类型: {kind}")
