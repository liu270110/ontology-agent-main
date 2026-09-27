"""L5 ontology_core · tbox：Turtle 制品装载/序列化 + IRI 校验（本体核心设计 §4 存储 / §4.1 命名空间规范）。

IRI 校验=命名空间前缀匹配：合法 IRI 必须落在「本体默认命名空间（iri_base）+ 平台级 ob2:/task: +
W3C 标准 NS」并集内；禁中文/空格（§4.1 禁止项：IRI 含中文/空格；发布后修改已有 IRI 由聚合 4201 拒）。
"""

from __future__ import annotations

from collections.abc import Iterable

from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, SH, SKOS, XSD

OB2 = Namespace("https://ontology-agent.dev/ns/ob2#")  # 平台顶类命名空间（§4.1：ob2:Object/Action/Event/Rule）
TASK = Namespace("https://ontology-agent.dev/ns/task#")  # 任务本体九类（§2 平台标准本体模块，对 MCP 只读）

# W3C 标准命名空间：R1/R2 规则与元数据声明落点（owl/rdfs/rdf/xsd/skos/shaql）
STANDARD_NAMESPACES: tuple[str, ...] = (str(RDF), str(RDFS), str(OWL), str(XSD), str(SKOS), str(SH))


def default_namespace(tenant_id: str, slug: str) -> str:
    """本体默认命名空间（§4.1：http://ontology-agent.local/o/{tenant_id}/{ontology_slug}#）。"""
    return f"http://ontology-agent.local/o/{tenant_id}/{slug}#"


def load_turtle(content: str | bytes) -> Graph:
    """Turtle → rdflib 内存图（编辑期权威载体，§4 rdflib 行）；解析失败抛 ValueError（原文出处随消息）。"""
    graph = Graph()
    try:
        graph.parse(data=content, format="turtle")
    except Exception as exc:  # rdflib 异常族不稳定，统一转 ValueError 保门禁可判
        raise ValueError(f"Turtle 解析失败: {exc}") from exc
    graph.bind("ob2", OB2)
    graph.bind("task", TASK)
    return graph


def serialize_turtle(graph: Graph) -> str:
    """图 → Turtle 制品文本（发布制品默认格式，§4 MinIO 行）。"""
    return graph.serialize(format="turtle")


def local_name(iri: str) -> str:
    """本地标识符：fragment（#后）或路径末段（§4.1 命名规范=命名空间+本地名）。"""
    return iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def iri_problems(iri: str, allowed_namespaces: Iterable[str]) -> list[str]:
    """IRI 校验（返回问题清单，空=合法）：
    ① 禁空白/非 ASCII（§4.1 禁止项）；② 命名空间前缀匹配（合法集=allowed_namespaces ∪ 标准 NS）。
    """
    problems: list[str] = []
    if any(ch.isspace() for ch in iri):
        problems.append("IRI 含空白字符（§4.1 禁止项）")
    if any(ord(ch) > 127 for ch in iri):
        problems.append("IRI 含非 ASCII 字符/中文（§4.1 禁止项）")
    allowed = {*allowed_namespaces, *STANDARD_NAMESPACES}
    if not any(iri.startswith(ns) for ns in allowed):
        problems.append(f"IRI 不在声明命名空间内（合法前缀={sorted(allowed)}）")
    return problems
