# tests/golden/test_gate_golden.py
"""golden 30 例门禁评估集（计划 2.1 残尾）：服务端硬门禁 run_changeset_gate 的结论确定性评估。

构成：15 合法 + 15 非法，全部参数化；合法例从种子本体（services/seeds/power_seed.ttl）的类/属性/规则
裁剪为最小 TTL 片段（用例注释标注种子出处），非法例覆盖 ≥9 类违规：

| 类别 | 违规码/约束组件 | 用例 |
| ---- | ---- | ---- |
| R1 公理封闭集（axiomKind 越表） | LINT_AXIOM_NOT_RL | X01 |
| R1 公理封闭集（DL 公理直接入图） | LINT_AXIOM_NOT_RL | X02 |
| R1 表达式非法（等价类非原子） | LINT_EXPR_NOT_RL | X03 |
| R2 未知 SHACL 算子（越集撞 R1 → 双路由） | LINT_ROUTE_DUAL | X04 |
| 双路由（R1+R3 双源禁止） | LINT_ROUTE_DUAL | X05 |
| R3 无路由证据 | LINT_ROUTE_UNKNOWN | X06 |
| R3 ECA 闭环判定拒绝（行动类反引缺失） | LINT_ACTION_NOT_CLOSED | X07 |
| 术语不唯一（同名多 IRI） | LINT_TERM_DUP | X08 |
| 重复 IRI（类/属性撞名，#/ 双拼） | LINT_TERM_DUP | X09 |
| SHACL sh:in 枚举违规 | sh:InConstraintComponent | X10 |
| SHACL sh:pattern 违规 | sh:PatternConstraintComponent | X11 |
| SHACL minCount 违规 | sh:MinCountConstraintComponent | X12 |
| profile 声明冲突 | LINT_PROFILE_CONFLICT | X13 |
| profile 声明非法值 | LINT_PROFILE_INVALID | X14 |
| Turtle 解析失败（缺 source_ref 兜底） | GATE_PARSE_FAILED | X15 |

每例断言门禁结论确定：conforms 布尔精确匹配 + 期望违规码/阶段命中 + 违规证据可追溯
（每条违规 path 非空，至少一条 source+path 均非空——X13 profile 冲突自身以 code 定位，
锚点由同制品内无路由规则违规提供）。客户端 gate_ok 不出现在本评估集：服务端从不信任自报
（宪法 3），结论只由制品内容决定。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from services.ontology.business.ontology_gate import GATE_VERSION, run_changeset_gate
from services.ontology.core import local_name

PWR = "http://ontology-agent.local/o/t1/power#"

_PREFIXES = """
@prefix ob2:  <https://ontology-agent.dev/ns/ob2#> .
@prefix pw:   <http://ontology-agent.local/o/t1/power#> .
@prefix task: <https://ontology-agent.dev/ns/task#> .
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix inst: <http://ontology-agent.local/o/t1/power/inst/> .
"""


def _ttl(fragment: str) -> str:
    """制品工厂：平台前缀头 + 用例最小片段（seed 同款命名空间，pw: = 种子默认命名空间）。"""
    return _PREFIXES + fragment


@dataclass(frozen=True)
class GoldenCase:
    """golden 单例：key（评估集编号+场景）+ 制品片段 + 期望门禁结论（conforms/违规码/阶段/路由）。"""

    key: str
    fragment: str
    conforms: bool
    code: str | None = None  # 期望违规码（lint 码 / SHACL 约束组件 IRI / GATE_PARSE_FAILED）
    stage: str | None = None  # 期望违规阶段（parse|lint|shacl）
    routes: dict[str, str] | None = None  # 期望路由判定（规则本地名 → 路由；合法例的确定性结论）


# ---------------------------------------------------------------- 合法 15 例（种子裁剪）

_LEGAL = (
    GoldenCase(
        key="合法-01-对象类层级",
        # 种子 §对象：pw:PowerDevice/Feeder/Transformer 层级（subClassOf 链入 ob2:Object 顶类）
        fragment="""
pw:PowerDevice a owl:Class ; rdfs:subClassOf ob2:Object ; rdfs:label "电力设备"@zh .
pw:Feeder a owl:Class ; rdfs:subClassOf pw:PowerDevice ; rdfs:label "馈线"@zh .
pw:Transformer a owl:Class ; rdfs:subClassOf pw:PowerDevice ; rdfs:label "变压器"@zh .
""",
        conforms=True,
    ),
    GoldenCase(
        key="合法-02-数据属性声明",
        # 种子 §属性：pw:orderNo（domain=OutageOrder，range=xsd:string）
        fragment="""
pw:OutageOrder a owl:Class ; rdfs:subClassOf ob2:Object ; rdfs:label "停电工单"@zh .
pw:orderNo a owl:DatatypeProperty ; rdfs:domain pw:OutageOrder ; rdfs:range xsd:string ;
    rdfs:label "工单编号"@zh .
""",
        conforms=True,
    ),
    GoldenCase(
        key="合法-03-对象属性声明",
        # 种子 §属性：pw:affectsFeeder（domain=OutageEvent，range=Feeder）
        fragment="""
pw:OutageEvent a owl:Class ; rdfs:subClassOf ob2:Event .
pw:Feeder a owl:Class ; rdfs:subClassOf ob2:Object .
pw:affectsFeeder a owl:ObjectProperty ; rdfs:domain pw:OutageEvent ; rdfs:range pw:Feeder ;
    rdfs:label "影响馈线"@zh .
""",
        conforms=True,
    ),
    GoldenCase(
        key="合法-04-R1互斥公理登记",
        # 种子 §规则 R001：axiomKind 登记 + owl:disjointWith 物化公理 → 路由 owl_axiom
        fragment="""
pw:Transformer a owl:Class ; rdfs:label "变压器"@zh .
pw:Meter a owl:Class ; rdfs:label "计量表"@zh .
pw:R001 a ob2:Rule ; rdfs:label "变压器与计量表互斥"@zh ; ob2:axiomKind "disjointWith" .
pw:Transformer owl:disjointWith pw:Meter .
""",
        conforms=True,
        routes={"R001": "owl_axiom"},
    ),
    GoldenCase(
        key="合法-05-R1层级公理登记",
        # 种子 §规则 R001 同款形态换 subClassOf kind（rl 封闭集内）
        fragment="""
pw:PowerDevice a owl:Class .
pw:Feeder a owl:Class ; rdfs:subClassOf pw:PowerDevice .
pw:R005 a ob2:Rule ; rdfs:label "馈线层级公理"@zh ; ob2:axiomKind "subClassOf" .
""",
        conforms=True,
        routes={"R005": "owl_axiom"},
    ),
    GoldenCase(
        key="合法-06-R1函数型属性公理",
        # 工单编号唯一（owl:FunctionalProperty，rl 封闭集内；R1 登记形态）
        fragment="""
pw:OutageOrder a owl:Class .
pw:orderNo a owl:DatatypeProperty, owl:FunctionalProperty ; rdfs:domain pw:OutageOrder .
pw:R006 a ob2:Rule ; rdfs:label "工单编号函数型"@zh ; ob2:axiomKind "functionalProperty" .
""",
        conforms=True,
        routes={"R006": "owl_axiom"},
    ),
    GoldenCase(
        key="合法-07-R1逆属性公理",
        # 派单/被派互逆（owl:inverseOf 在 rl 封闭集，种子 §规则 R1 形态）
        fragment="""
pw:OutageOrder a owl:Class .
pw:RepairCrew a owl:Class .
pw:dispatches a owl:ObjectProperty ; rdfs:domain pw:OutageOrder ; rdfs:range pw:RepairCrew .
pw:dispatchedBy a owl:ObjectProperty ; owl:inverseOf pw:dispatches .
pw:R007 a ob2:Rule ; rdfs:label "派单互逆公理"@zh ; ob2:axiomKind "inverseOf" .
""",
        conforms=True,
        routes={"R007": "owl_axiom"},
    ),
    GoldenCase(
        key="合法-08-R1主键公理",
        # 工单以编号为主键（owl:hasKey 在 rl 封闭集）
        fragment="""
pw:OutageOrder a owl:Class .
pw:orderNo a owl:DatatypeProperty .
pw:R008 a ob2:Rule ; rdfs:label "工单编号主键"@zh ; ob2:axiomKind "hasKey" .
pw:OutageOrder owl:hasKey ( pw:orderNo ) .
""",
        conforms=True,
        routes={"R008": "owl_axiom"},
    ),
    GoldenCase(
        key="合法-09-R2状态枚举shape",
        # 种子 §规则 R002：工单状态受控词表（sh:in + minCount，无实例 → SHACL 自校验通过）
        fragment="""
pw:OutageOrder a owl:Class ; rdfs:label "停电工单"@zh .
pw:hasStatus a owl:DatatypeProperty ; rdfs:domain pw:OutageOrder .
pw:R002 a ob2:Rule, sh:NodeShape ; rdfs:label "工单状态枚举约束"@zh ;
    sh:targetClass pw:OutageOrder ;
    sh:property [
        sh:path pw:hasStatus ;
        sh:in ( "created" "dispatched" "in_progress" "resolved" "closed" ) ;
        sh:minCount 1 ;
    ] .
""",
        conforms=True,
        routes={"R002": "shacl"},
    ),
    GoldenCase(
        key="合法-10-R2编号格式shape",
        # 种子 §规则 R004：工单编号格式（datatype+pattern+min/maxCount 全在 R2 算子封闭集）
        fragment="""
pw:OutageOrder a owl:Class .
pw:orderNo a owl:DatatypeProperty ; rdfs:domain pw:OutageOrder .
pw:R004 a ob2:Rule, sh:NodeShape ; rdfs:label "工单编号格式约束"@zh ;
    sh:targetClass pw:OutageOrder ;
    sh:property [
        sh:path pw:orderNo ;
        sh:datatype xsd:string ;
        sh:pattern "^OO-[0-9]{6}$" ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
    ] .
""",
        conforms=True,
        routes={"R004": "shacl"},
    ),
    GoldenCase(
        key="合法-11-R2双shape合规实例",
        # 合规实例（状态/编号均落枚举与格式）——制品兼 shapes 与 data，自校验通过
        fragment="""
pw:OutageOrder a owl:Class .
pw:hasStatus a owl:DatatypeProperty .
pw:orderNo a owl:DatatypeProperty .
pw:R002 a ob2:Rule, sh:NodeShape ;
    sh:targetClass pw:OutageOrder ;
    sh:property [ sh:path pw:hasStatus ; sh:in ( "created" "dispatched" ) ] .
pw:R004 a ob2:Rule, sh:NodeShape ;
    sh:targetClass pw:OutageOrder ;
    sh:property [ sh:path pw:orderNo ; sh:pattern "^OO-[0-9]{6}$" ] .
inst:OO-000001 a pw:OutageOrder ; pw:orderNo "OO-000001" ; pw:hasStatus "created" .
""",
        conforms=True,
        routes={"R002": "shacl", "R004": "shacl"},
    ),
    GoldenCase(
        key="合法-12-R3完整ECA",
        # 种子 §规则 R003：停电确认→派发抢修（ASK 条件+事件类+行动类+行动闭环反引齐备）
        fragment="""
pw:OutageConfirmed a owl:Class ; rdfs:subClassOf ob2:Event ; rdfs:label "停电确认事件"@zh .
pw:DispatchRepair a owl:Class ; rdfs:subClassOf ob2:Action ; rdfs:label "派发抢修"@zh ;
    ob2:triggeredByEvent pw:OutageConfirmed ; ob2:guardedByRule pw:R003 ;
    ob2:executionMode "stateful" ; ob2:deterministic false .
pw:R003 a ob2:Rule ; rdfs:label "停电确认→派发抢修工单"@zh ;
    ob2:eventClass pw:OutageConfirmed ;
    ob2:condition "ASK WHERE { ?e a pw:OutageConfirmed }"^^ob2:sparqlAsk ;
    ob2:actionRef pw:DispatchRepair .
""",
        conforms=True,
        routes={"R003": "engine"},
    ),
    GoldenCase(
        key="合法-13-R3双事件ECA",
        # 种子 §规则 R005/R006：风暴预警→隔离、抢修完成→送电（行动类闭环反引各自齐备）
        fragment="""
pw:StormAlert a owl:Class ; rdfs:subClassOf ob2:Event .
pw:RepairCompleted a owl:Class ; rdfs:subClassOf ob2:Event .
pw:IsolateFault a owl:Class ; rdfs:subClassOf ob2:Action ;
    ob2:triggeredByEvent pw:StormAlert ; ob2:guardedByRule pw:R005 ;
    ob2:executionMode "stateful" ; ob2:deterministic true .
pw:RestorePower a owl:Class ; rdfs:subClassOf ob2:Action ;
    ob2:triggeredByEvent pw:RepairCompleted ; ob2:guardedByRule pw:R006 ;
    ob2:executionMode "externalWrite" ; ob2:deterministic true .
pw:R005 a ob2:Rule ; rdfs:label "风暴预警→故障隔离"@zh ;
    ob2:eventClass pw:StormAlert ;
    ob2:condition "ASK WHERE { ?e a pw:StormAlert }"^^ob2:sparqlAsk ;
    ob2:actionRef pw:IsolateFault .
pw:R006 a ob2:Rule ; rdfs:label "抢修完成→恢复送电"@zh ;
    ob2:eventClass pw:RepairCompleted ;
    ob2:condition "ASK WHERE { ?e a pw:RepairCompleted }"^^ob2:sparqlAsk ;
    ob2:actionRef pw:RestorePower .
""",
        conforms=True,
        routes={"R005": "engine", "R006": "engine"},
    ),
    GoldenCase(
        key="合法-14-本体头profile声明",
        # 种子 §本体头：owl:Ontology + ob2:profile "rl" + versionInfo（声明合法且在封闭集）
        fragment="""
<http://ontology-agent.local/o/t1/power> a owl:Ontology ;
    rdfs:label "电力配电网停电分析种子本体"@zh ;
    ob2:profile "rl" ;
    owl:versionInfo "v1" .
pw:Feeder a owl:Class ; rdfs:subClassOf ob2:Object .
""",
        conforms=True,
    ),
    GoldenCase(
        key="合法-15-跨命名空间同名隔离",
        # 种子 §任务本体：pw:/task: 分命名空间隔离——同名术语跨命名空间不构成重复（§2 平台设计）
        fragment="""
pw:Task a owl:Class ; rdfs:subClassOf ob2:Object .
task:Task a owl:Class ; rdfs:label "任务"@zh .
pw:hasStatus a owl:DatatypeProperty ; rdfs:domain pw:Task .
task:hasStatus a owl:DatatypeProperty ; rdfs:domain task:Task .
""",
        conforms=True,
    ),
)

# ---------------------------------------------------------------- 非法 15 例（≥9 类违规）

_ILLEGAL = (
    GoldenCase(
        key="非法-01-R1公理封闭集越表",
        # axiomKind "cardinality" 不在 rl 允许公理封闭集——人工改判越表，编辑期拒绝（§2.3）
        fragment='pw:R001 a ob2:Rule ; ob2:axiomKind "cardinality" .',
        conforms=False,
        code="LINT_AXIOM_NOT_RL",
        stage="lint",
    ),
    GoldenCase(
        key="非法-02-DL公理直接入图",
        # owl:someValuesFrom 属 DL 公理家族，rl 档本体直接拒绝（02 篇 §3.1）
        fragment="""
pw:Feeder a owl:Class .
pw:InspectionTask owl:someValuesFrom pw:Feeder .
""",
        conforms=False,
        code="LINT_AXIOM_NOT_RL",
        stage="lint",
    ),
    GoldenCase(
        key="非法-03-等价类非原子表达式",
        # equivalentClass 两侧须原子类——unionOf 限制表达式不入 R1（§2.3 RL 合法性）
        fragment="""
pw:Feeder a owl:Class .
pw:Transformer a owl:Class .
pw:Meter a owl:Class .
pw:Feeder owl:equivalentClass [ owl:unionOf ( pw:Transformer pw:Meter ) ] .
""",
        conforms=False,
        code="LINT_EXPR_NOT_RL",
        stage="lint",
    ),
    GoldenCase(
        key="非法-04-R2未知SHACL算子",
        # sh:not 不在 R2 算子封闭集 → 判定表④转 R3；同时存在 R1 axiomKind 登记 → 双路由拒绝
        fragment="""
pw:Transformer a owl:Class .
pw:R002 a ob2:Rule, sh:NodeShape ; ob2:axiomKind "subClassOf" ;
    sh:targetClass pw:Transformer ;
    sh:property [ sh:path rdfs:label ; sh:not [ sh:datatype xsd:integer ] ] .
""",
        conforms=False,
        code="LINT_ROUTE_DUAL",
        stage="lint",
    ),
    GoldenCase(
        key="非法-05-双路由R1加R3",
        # 同一规则 axiomKind（R1）+ eventClass（R3）双证据——双源禁止（§2.3）
        fragment="""
pw:OutageConfirmed a owl:Class ; rdfs:subClassOf ob2:Event .
pw:R001 a ob2:Rule ; ob2:axiomKind "disjointWith" ; ob2:eventClass pw:OutageConfirmed .
""",
        conforms=False,
        code="LINT_ROUTE_DUAL",
        stage="lint",
    ),
    GoldenCase(
        key="非法-06-规则无路由证据",
        # 规则无 axiomKind/shape/ECA 任一证据 → 路由不可判定（LINT_ROUTE_UNKNOWN）
        fragment='pw:R999 a ob2:Rule ; rdfs:label "无证据规则"@zh .',
        conforms=False,
        code="LINT_ROUTE_UNKNOWN",
        stage="lint",
    ),
    GoldenCase(
        key="非法-07-ECA行动闭环断裂",
        # 行动类缺 triggeredByEvent/guardedByRule 反向引用——逆向闭环断裂（§2.4）
        fragment="""
pw:OutageConfirmed a owl:Class ; rdfs:subClassOf ob2:Event .
pw:DispatchRepair a owl:Class ; rdfs:subClassOf ob2:Action .
pw:R003 a ob2:Rule ; ob2:eventClass pw:OutageConfirmed ; ob2:actionRef pw:DispatchRepair .
""",
        conforms=False,
        code="LINT_ACTION_NOT_CLOSED",
        stage="lint",
    ),
    GoldenCase(
        key="非法-08-术语不唯一同名多IRI",
        # 同命名空间域同名多 IRI（#/ 双拼）：一个概念只有一个规范名（§2.3 术语唯一性）
        fragment="""
pw:PowerDevice a owl:Class .
<http://ontology-agent.local/o/t1/power/Meter> a owl:Class .
pw:Meter a owl:Class ; rdfs:label "计量表"@zh .
""",
        conforms=False,
        code="LINT_TERM_DUP",
        stage="lint",
    ),
    GoldenCase(
        key="非法-09-重复IRI类属性撞名",
        # 同名 IRI 一处登记为类、一处（路径式双拼）登记为数据属性——术语撞名重复登记
        fragment="""
pw:Meter a owl:Class ; rdfs:label "计量表"@zh .
<http://ontology-agent.local/o/t1/power/Meter> a owl:DatatypeProperty .
""",
        conforms=False,
        code="LINT_TERM_DUP",
        stage="lint",
    ),
    GoldenCase(
        key="非法-10-SHACL状态枚举违规",
        # R002 形态 shape：实例状态 "flying" 不在 sh:in 受控词表 → InConstraintComponent
        fragment="""
pw:OutageOrder a owl:Class .
pw:hasStatus a owl:DatatypeProperty .
pw:R002 a ob2:Rule, sh:NodeShape ; sh:targetClass pw:OutageOrder ;
    sh:property [ sh:path pw:hasStatus ; sh:in ( "created" "dispatched" "closed" ) ] .
inst:OO-000002 a pw:OutageOrder ; pw:hasStatus "flying" .
""",
        conforms=False,
        code="http://www.w3.org/ns/shacl#InConstraintComponent",
        stage="shacl",
    ),
    GoldenCase(
        key="非法-11-SHACL编号格式违规",
        # R004 形态 shape：编号 "WRONG-1" 不匹配 sh:pattern → PatternConstraintComponent
        fragment="""
pw:OutageOrder a owl:Class .
pw:orderNo a owl:DatatypeProperty .
pw:R004 a ob2:Rule, sh:NodeShape ; sh:targetClass pw:OutageOrder ;
    sh:property [ sh:path pw:orderNo ; sh:pattern "^OO-[0-9]{6}$" ] .
inst:OO-000003 a pw:OutageOrder ; pw:orderNo "WRONG-1" .
""",
        conforms=False,
        code="http://www.w3.org/ns/shacl#PatternConstraintComponent",
        stage="shacl",
    ),
    GoldenCase(
        key="非法-12-SHACL必填缺失违规",
        # shape 要求 orderNo minCount 1，实例缺编号 → MinCountConstraintComponent
        fragment="""
pw:OutageOrder a owl:Class .
pw:orderNo a owl:DatatypeProperty .
pw:R004 a ob2:Rule, sh:NodeShape ; sh:targetClass pw:OutageOrder ;
    sh:property [ sh:path pw:orderNo ; sh:minCount 1 ] .
inst:OO-000004 a pw:OutageOrder ; pw:hasStatus "created" .
""",
        conforms=False,
        code="http://www.w3.org/ns/shacl#MinCountConstraintComponent",
        stage="shacl",
    ),
    GoldenCase(
        key="非法-13-profile声明冲突",
        # 本体头双 profile 声明（rl+dl）→ LINT_PROFILE_CONFLICT；该违规以 code 定位
        # （subject 缺席为已知形态），制品内无路由规则违规提供 source 锚点满足可追溯断言
        fragment="""
<http://ontology-agent.local/o/t1/power> a owl:Ontology ; ob2:profile "rl", "dl" .
pw:R999 a ob2:Rule ; rdfs:label "无证据规则（可追溯锚点）"@zh .
""",
        conforms=False,
        code="LINT_PROFILE_CONFLICT",
        stage="lint",
    ),
    GoldenCase(
        key="非法-14-profile声明非法值",
        # profile 必须为 rl|el|dl 之一，"ql" 非法 → LINT_PROFILE_INVALID（subject=非法值）
        fragment="""
<http://ontology-agent.local/o/t1/power> a owl:Ontology ; ob2:profile "ql" .
""",
        conforms=False,
        code="LINT_PROFILE_INVALID",
        stage="lint",
    ),
    GoldenCase(
        key="非法-15-Turtle解析失败缺source_ref兜底",
        # 制品语法非法（三元组残缺）：无任何术语可定位（缺 source_ref）——门禁 fail-closed
        # 合成 <artifact>/turtle 兜底指针，违规结论仍可追溯（GATE_PARSE_FAILED）
        fragment="pw:Feeder a owl:Class ; rdfs:subClassOf",
        conforms=False,
        code="GATE_PARSE_FAILED",
        stage="parse",
    ),
)

_CASES = _LEGAL + _ILLEGAL


# ---------------------------------------------------------------- golden 评估入口


@pytest.mark.parametrize("case", _CASES, ids=[c.key for c in _CASES])
async def test_golden_gate_门禁结论确定(case: GoldenCase) -> None:
    # Arrange —— 制品工厂：平台前缀头 + golden 最小片段
    turtle = _ttl(case.fragment)
    # Act —— 服务端硬门禁实跑（lint 三路由 + SHACL 自校验，制品兼 shapes 与 data）
    report = await run_changeset_gate(turtle)
    # Assert —— 门禁结论确定：conforms 布尔精确匹配；gate 版本随报告留痕
    assert report.conforms is case.conforms
    assert report.gate_version == GATE_VERSION
    if case.code is not None:
        matched = [v for v in report.violations if v.code == case.code]
        assert matched, f"期望违规码 {case.code} 未出现，实得: {[v.code for v in report.violations]}"
        assert all(v.stage == case.stage for v in matched)  # 违规阶段确定
        assert all(v.message for v in matched)  # 违规证据随报告留痕
        # 证据可追溯：每条违规携带非空定位指针（path），至少一条 source+path 均非空
        assert all(v.path for v in report.violations)
        assert any(v.source and v.path for v in report.violations)
    if case.routes is not None:
        assert {local_name(iri): route for iri, route in report.routes.items()} == case.routes
    if case.conforms:
        assert report.violations == []  # 合法例：零违规零告警


def test_golden_gate_评估集规模与分布() -> None:
    """评估集元断言：30 例整（15 合法 + 15 非法）、编号唯一、非法例必须声明期望违规码与阶段。"""
    # Assert
    assert len(_CASES) == 30
    assert len({c.key for c in _CASES}) == 30
    assert sum(1 for c in _CASES if c.conforms) == 15
    illegal = [c for c in _CASES if not c.conforms]
    assert len(illegal) == 15
    assert all(c.code is not None and c.stage in ("parse", "lint", "shacl") for c in illegal)
    assert PWR in _PREFIXES  # 评估集锚定种子默认命名空间
