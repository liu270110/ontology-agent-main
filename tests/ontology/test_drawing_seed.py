"""A4 电力图纸本体 v0 种子（services/seeds/drawing_seed.ttl@v1）用例。

仿 power_seed 先例：种子解析 + lint 全绿（三路由判定，tests/ontology/test_ontology.py §种子本体）、
SHACL 门禁正反例（合法 Drawing 通过 / 缺图号拒绝 / 版本链约束）、装载自检冒烟（seed_service 同款
inspect_seed）、装载目录 + kb_align 一级术语对齐链路（tests/kb/test_kb_extraction.py §纯函数先例）。
全部样例标识一律 SAMPLE-* 占位（standards/02 §11 脱敏红线：零真实图号/项目标识）。
"""

from __future__ import annotations

from pathlib import Path

from rdflib import RDF, Graph, URIRef
from rdflib.namespace import OWL, SH

from services.kb.business.kb_extraction import load_seed_catalog, match_seed_class
from services.ontology.business.seed_service import inspect_seed
from services.ontology.core.lint import lint
from services.ontology.core.shacl import validate
from services.ontology.core.tbox import load_turtle

DRAWING_SEED_PATH = Path(__file__).resolve().parents[2] / "services" / "seeds" / "drawing_seed.ttl"
DRW = "http://ontology-agent.local/o/t1/drawing#"  # 自持命名空间（本体核心设计 §4.1，t1/drawing 种子占位）

_CLASSES = (
    "Drawing",
    "DrawingSheet",
    "Part",
    "Material",
    "Dimension",
    "TechRequirement",
    "DrawingRevision",
)
_RULES = ("R001", "R002", "R003", "R004", "R005")

# 合法「图纸全链」正例（SAMPLE-* 占位）：标题栏 + 图幅 + 版本链头 + BOM 行 + 尺寸 + 技术要求
_GOOD_DRAWING = """
@prefix drw:  <http://ontology-agent.local/o/t1/drawing#> .
@prefix inst: <http://ontology-agent.local/kb/fact/sample/> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

inst:DRAWING-SAMPLE-001 a drw:Drawing ;
    drw:drawingNo "SAMPLE-DWG-001" ;
    drw:drawingName "样例一次接线图" ;
    drw:drawingType "一次接线图" ;
    drw:hasSheet inst:SHEET-SAMPLE-1 ;
    drw:hasRevision inst:REV-SAMPLE-0 ;
    drw:hasPart inst:PART-SAMPLE-1 ;
    drw:hasTechRequirement inst:TR-SAMPLE-1 .

inst:SHEET-SAMPLE-1 a drw:DrawingSheet ;
    drw:sheetSize "A4" ; drw:scale "1:50" .

inst:REV-SAMPLE-0 a drw:DrawingRevision ;
    drw:revisionNo "0" ;
    drw:revisionOf inst:DRAWING-SAMPLE-001 ;
    drw:designedBy "样例设计人" ;
    drw:designDate "2026-01-01"^^xsd:date ;
    drw:checkedBy "样例审核人" .

inst:PART-SAMPLE-1 a drw:Part ;
    drw:quantity 2 ; drw:weight 1.25 ;
    drw:ofMaterial inst:MAT-SAMPLE-1 ;
    drw:hasDimension inst:DIM-SAMPLE-1 .

inst:MAT-SAMPLE-1 a drw:Material ; drw:materialGrade "SAMPLE-Q235B" .
inst:DIM-SAMPLE-1 a drw:Dimension ; drw:dimensionValue 100.00 ; drw:dimensionUnit "mm" .
inst:TR-SAMPLE-1 a drw:TechRequirement ; drw:requirementText "样例技术要求：安装前核对图号。" .
"""


def _seed_graph() -> Graph:
    return load_turtle(DRAWING_SEED_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 解析 + lint 门禁


def test_种子解析与lint全绿_引用形态落位():
    # Arrange：种子资产落位 services/seeds/（清单引用形态 = 相对路径 + owl:versionInfo）
    assert DRAWING_SEED_PATH.exists()
    assert DRAWING_SEED_PATH.relative_to(DRAWING_SEED_PATH.parents[2]).as_posix() == "services/seeds/drawing_seed.ttl"
    # Act：装载 + lint（三路由判定表，本体核心设计 §2.3）
    graph = _seed_graph()
    report = lint(graph)
    # Assert：头声明（v1 版本化 + rl 档）、7 类齐备、五规则全部判 R2 shacl、lint 零违规
    header = graph.value(URIRef(DRW[:-1]), URIRef(f"{OWL}versionInfo"))
    assert header is not None and str(header) == "v1"
    assert str(graph.value(URIRef(DRW[:-1]), URIRef("https://ontology-agent.dev/ns/ob2#profile"))) == "rl"
    classes = {str(c) for c in graph.subjects(RDF.type, OWL.Class) if str(c).startswith(DRW)}
    assert classes == {f"{DRW}{name}" for name in _CLASSES}
    assert report.ok, [v.model_dump() for v in report.violations]
    assert report.profile == "rl"
    assert report.routes == {f"{DRW}{rule}": "shacl" for rule in _RULES}
    for rule in _RULES:  # ob2:route 标注与 lint 判定一致（人工改判越表即违规，§2.3）
        assert str(graph.value(URIRef(f"{DRW}{rule}"), URIRef("https://ontology-agent.dev/ns/ob2#route"))) == "shacl"


def test_种子装载自检冒烟():
    # Arrange / Act：装载 + 自检一步（seed_service.load_seed_report 同款 inspect_seed 口径）
    graph = _seed_graph()
    report = inspect_seed(graph)
    # Assert：类计数含平台顶类最小集（ob2:Object/Rule），行动类 0（v0 对象层种子）、shape 5、lint 过
    assert (report.class_count, report.action_count, report.shape_count) == (9, 0, 5)
    assert report.lint_ok and report.lint_violations == []


# ---------------------------------------------------------------- SHACL 门禁（引擎 2，失败即拒绝）


def test_SHACL门禁_合法图纸全链通过():
    # Arrange：种子图作 shapes + 合法样例图（SAMPLE-* 占位）
    shapes = _seed_graph()
    good = load_turtle(_GOOD_DRAWING)
    # Act：pySHACL 约束验证（位置传参——shacl.py 关键字路径有静默不校验缺陷）
    report = validate(good, shapes, tbox_graph=shapes)
    # Assert：合规且零违例（图号/图名/图幅/版本链/BOM/尺寸全链过门禁）
    assert report.conforms, [r.model_dump() for r in report.results]
    assert report.results == []


def test_SHACL门禁_缺图号被拒():
    # Arrange：合法样例抽掉 drawingNo（R001 minCount 1 应命中且可追溯）
    bad = load_turtle(_GOOD_DRAWING.replace('    drw:drawingNo "SAMPLE-DWG-001" ;\n', ""))
    # Act / Assert：拒绝 + 违规指针=图号属性 + MinCount 约束分量
    report = validate(bad, _seed_graph(), tbox_graph=_seed_graph())
    assert not report.conforms
    drawing_no_hits = [r for r in report.results if r.path == f"{DRW}drawingNo"]
    assert drawing_no_hits, [r.model_dump() for r in report.results]
    assert all(r.constraint == f"{SH}MinCountConstraintComponent" for r in drawing_no_hits)


def test_SHACL门禁_版本链双前驱与缺所属图纸被拒():
    # Arrange：正例追加第二前驱（多父分叉）并抽掉 revisionOf（链必挂图纸）
    graph_text = _GOOD_DRAWING.replace(
        "    drw:revisionOf inst:DRAWING-SAMPLE-001 ;\n",
        "    drw:previousRevision inst:REV-SAMPLE-9 , inst:REV-SAMPLE-8 ;\n",
    )
    bad = load_turtle(graph_text)
    shapes = _seed_graph()
    # Act
    report = validate(bad, shapes, tbox_graph=shapes)
    # Assert：前驱 maxCount=1 与所属图纸 minCount=1 双双拒绝（单链约束闭环）
    assert not report.conforms
    paths = {(r.path, r.constraint) for r in report.results}
    assert (f"{DRW}previousRevision", f"{SH}MaxCountConstraintComponent") in paths, [
        r.model_dump() for r in report.results
    ]
    assert (f"{DRW}revisionOf", f"{SH}MinCountConstraintComponent") in paths


# ---------------------------------------------------------------- 装载目录 + kb_align 对齐链路


def test_装载目录冒烟_seed_catalog():
    # Arrange / Act：kb_align 同款装载链（load_seed_catalog 参数化路径）
    catalog = load_seed_catalog(DRAWING_SEED_PATH)
    # Assert：目录口径=全图术语（机制实况：含 ob2: 顶类最小集重声明）9 类 25 属性；drw 域内 7 类 23 属性；
    # 全图作 shapes、关键术语在册（类 IRI 白名单 = 三级对齐边界）
    assert len(catalog.classes) == 9 and len(catalog.properties) == 25
    assert {iri for iri, _, _ in catalog.classes if iri.startswith(DRW)} == {f"{DRW}{n}" for n in _CLASSES}
    drw_properties = [iri for iri, _, _ in catalog.properties if iri.startswith(DRW)]
    assert len(drw_properties) == 23
    assert f"{DRW}Drawing" in catalog.class_iris
    labels = {iri: label for iri, label, _ in catalog.properties}
    assert labels[f"{DRW}drawingNo"] == "图号" and labels[f"{DRW}ofMaterial"] == "材料"
    assert len(catalog.shapes_graph) == len(_seed_graph())


def test_对齐链路_一级术语对齐命中与跨域不误命中():
    # Arrange：drawing 种子目录（同 match_seed_class 先例纯函数口径）
    catalog = load_seed_catalog(DRAWING_SEED_PATH)
    # Act / Assert：精确（中文标签/本地名小写）→ 包含（去空格小写）→ 未命中保留待审
    assert match_seed_class("图纸", catalog) == (f"{DRW}Drawing", "exact")
    assert match_seed_class("drawingrevision", catalog) == (f"{DRW}DrawingRevision", "exact")
    assert match_seed_class("零件表 SAMPLE", catalog) == (f"{DRW}Part", "contains")
    assert match_seed_class("材料牌号SAMPLE-Q235B", catalog) == (f"{DRW}Material", "contains")
    # 跨域守卫：停电域术语（属 pw: 种子）不得误命中图纸种子（分命名空间隔离，§2）
    assert match_seed_class("馈线F001", catalog) is None
    assert match_seed_class("变电站", catalog) is None
    assert match_seed_class("   ", catalog) is None
