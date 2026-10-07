# tests/ontology/test_glossary_seed.py
"""K4 Glossary 业务术语层用例（docs/Agent/13 §9 K4-a；三字段模型=tis@18 §10.1）。

断言目标：
- 建模：GLOSS 命名空间常量入 tbox（对齐既有平台 NS 风格）；种子 gloss:Term 类 +
  gloss:target 定位属性（domain/range 声明）+ ≥3 电力域术语实例（label/altLabel/target 齐备）；
- 装载：种子经 tbox 规范入口可装载，gloss 前缀随装载绑定。
零外部依赖：纯 rdflib 内存图 + 种子资产（同 tests/ontology 夹具纪律；种子装载/目录消费的
kb 侧用例见 tests/kb/test_glossary_kb.py——本文件不 import kb 模块，建模资产归本体侧自证）。
"""

from __future__ import annotations

from pathlib import Path

from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from services.ontology.core import GLOSS
from services.ontology.core.tbox import load_turtle

PW = Namespace("http://ontology-agent.local/o/t1/power#")
SEED_TTL = Path(__file__).resolve().parents[2] / "services" / "seeds" / "power_seed.ttl"


def _seed_graph() -> Graph:
    return load_turtle(SEED_TTL.read_text(encoding="utf-8"))


def test_GLOSS命名空间常量_对齐既有平台NS风格() -> None:
    """K4-a：GLOSS 入 tbox 常量面并经 core 公开（与 OB2/TASK 同款平台级 NS）。"""
    assert str(GLOSS) == "https://ontology-agent.dev/ns/gloss#"


def test_种子术语建模_glossTerm类与target定位属性() -> None:
    """gloss:Term=术语类（label「业务术语」）；gloss:target=定位属性，domain=gloss:Term（range 兼容类/属性）。"""
    g = _seed_graph()
    assert (GLOSS.Term, RDF.type, OWL.Class) in g
    assert str(g.value(GLOSS.Term, RDFS.label)) == "业务术语"
    assert (GLOSS.target, RDF.type, OWL.ObjectProperty) in g
    assert g.value(GLOSS.target, RDFS.domain) == GLOSS.Term
    assert g.value(GLOSS.target, RDFS.range) is not None  # range 声明在场（rdfs:Resource，RL 档兼容）


def test_种子术语实例_至少3条且三字段齐备() -> None:
    """≥3 电力域术语实例：zh 规范术语 + ≥1 别名 + gloss:target 指向图内真实存在的本体类/属性 IRI。"""
    g = _seed_graph()
    iris_in_graph = {str(s) for s in g.subjects()}
    instances = list(g.subjects(RDF.type, GLOSS.Term))
    assert len(instances) >= 3, f"术语实例不足 3 条: {len(instances)}"
    for term in instances:
        label = g.value(term, RDFS.label)
        assert label is not None and label.language == "zh", f"{term} 缺 zh 规范术语"
        aliases = list(g.objects(term, SKOS.altLabel))
        assert aliases, f"{term} 缺 skos:altLabel 别名"
        target = g.value(term, GLOSS.target)
        assert target is not None, f"{term} 缺 gloss:target 定位"
        assert str(target) in iris_in_graph, f"{term} 的 target {target} 不在种子图内（悬空定位）"
        assert str(target).startswith(str(PW)), f"{term} 的 target {target} 不在种子场景命名空间"


def test_种子经tbox装载_gloss前缀绑定() -> None:
    """种子经 tbox.load_turtle（规范入口）装载成功且 gloss 前缀随装载绑定（序列化可读回）。"""
    g = _seed_graph()
    assert any(str(s).startswith(str(GLOSS)) for s in g.subjects()), "装载后无 gloss 命名空间主体"
    assert any(str(ns) == str(GLOSS) for _prefix, ns in g.namespaces()), "gloss 前缀未随装载绑定"
