"""L5 ontology_core · query：只读 SPARQL 查询面（api/01 §5.3 query 行；本体核心设计 §7.2 查询面）。

只读护栏：语句类型在 rdflib 代数层白名单校验——仅 SELECT / ASK；SPARQL Update 语法
（INSERT/DELETE/LOAD 等）在 rdflib 查询解析器即拒绝（查询文法不含更新语句），CONSTRUCT/DESCRIBE
本面亦不支持（2026-09-28 最小闭环裁决：收窄为 SELECT/ASK）。执行超时护栏由调用方
asyncio.wait_for + 客户端 timeout_ms 预算强制（本模块纯同步计算，to_thread 归 L2 编排）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from pydantic import BaseModel, Field
from rdflib import Graph
from rdflib.plugins.sparql import prepareQuery
from rdflib.term import BNode

READONLY_FORMS: frozenset[str] = frozenset({"SelectQuery", "AskQuery"})
MAX_QUERY_ROWS = 200  # 行数封顶（防全量导出打爆响应面；分页/投影下推随 M3+ 检索面）


class SparqlRejected(ValueError):
    """SPARQL 语句被只读面拒绝（语法错误或语句类型越白名单；调用方转 3001/422）。"""


class QueryResult(BaseModel):
    """查询结果（select=变量+字符串化行，ask=布尔；行数封顶 truncated 标记）。"""

    form: str = Field(pattern="^(select|ask)$")
    variables: list[str] = Field(default_factory=list)
    rows: list[dict[str, str | None]] = Field(default_factory=list)
    boolean: bool | None = None
    truncated: bool = False


def prepare_readonly(sparql: str) -> tuple[Any, str]:
    """解析 + 语句类型白名单校验（执行前判定，注入防护主闸）：返回 (prepared_query, form)。

    校验先行于执行——恶意/误用语句在进入 rdflib 求值器之前即被拒绝（api/01 §5.3 注入防护行）。
    """
    try:
        prepared = prepareQuery(sparql)
    except Exception as exc:  # rdflib 解析异常族不稳定，统一转 SparqlRejected 保 3001 可判
        raise SparqlRejected(f"SPARQL 语法错误: {exc}") from exc
    form = prepared.algebra.name
    if form not in READONLY_FORMS:
        raise SparqlRejected(
            f"只读面仅允许 SELECT/ASK 查询（得 {form}）——INSERT/DELETE 等改写与图构造语义被拒绝"
        )
    return prepared, ("select" if form == "SelectQuery" else "ask")


def execute_readonly(graph: Graph, prepared: Any, form: str, *, limit: int = MAX_QUERY_ROWS) -> QueryResult:
    """执行已校验的只读查询并序列化结果（同步计算，调用方 to_thread 包裹 + wait_for 预算）。

    序列化口径：IRI/literal 一律字符串化（字面量取词法形式），空白节点 `_:label`——DTO 层零
    rdflib 类型依赖。
    """
    result = graph.query(prepared)
    if form == "ask":
        return QueryResult(form="ask", boolean=bool(result.askAnswer))
    variables = [str(var) for var in (result.vars or [])]
    rows: list[dict[str, str | None]] = []
    truncated = False
    for row in result:
        if len(rows) >= limit:
            truncated = True
            break
        binding = cast("Mapping[str, Any]", row)  # ResultRow 按变量名字符串取列
        rows.append({name: _serialize_term(binding[name]) for name in variables})
    return QueryResult(form="select", variables=variables, rows=rows, truncated=truncated)


def _serialize_term(node: Any) -> str | None:
    """rdflib 项 → 字符串（字面量取词法形式；空白节点 `_:label` 前缀防与 IRI 混淆）。"""
    if node is None:
        return None
    if isinstance(node, BNode):
        return f"_:{node}"
    return str(node)
