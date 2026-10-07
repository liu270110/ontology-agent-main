# tests/kb/test_glossary_wiring.py
"""K8 检索第 4 路生产接线用例（docs/Agent/13 §14；K4 P2③ 收口）。

断言目标：
- K8-a glossary_recall：targets ∩ 层次读模型守卫（stale target / 空层次不进 SQL，零查询返回
  空）；hop-0 召回复用 _ADJACENT_CHUNKS_SQL（谓词与图路逐跳同款，ACL 片段/参数、bi-temporal
  时点下推），结果与 bm25/vector 同构（SearchHit 全字段投影）；
- K8-b 共享构造器（search_service）：目录空 → None（第 4 路不启用零干扰）；词面解析纯函数
  （别名/label 精确命中、声明序去重、无命中零 SQL）；有命中 → 词面→gloss:target→召回全链；
- K8-c 生产接线真可达：假目录经 agentic 单例注入后，KnowledgeSearchService.search
  （search_service 调用点）与 /kb/search 路由（api/kb.py 非 agentic + agentic 两调用点）
  单查命中 → 第 4 路进 RRF 融合（channels 含 glossary、citations 带 chunk）。

环境纪律：零外部依赖（假目录 + 桩会话 + 注入降级嵌入；不触 DB/嵌入服务/真实种子装载——
单例缓存位直接注入，同 K4 测试「不依赖 develop 破损 import 面」纪律）。
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from rdflib import Graph

import services.kb.business.agentic as agentic_mod
from services.kb.api.kb import search as kb_search_route
from services.kb.api.schemas.kb import KbSearchIn
from services.kb.business.kb_extraction import GlossaryTerm, SeedCatalog
from services.kb.business.search_service import (
    KnowledgeSearchService,
    build_glossary_recall_fn,
    glossary_targets_for_query,
)
from services.kb.retrieval.embed import AclPushdown, EmbeddingUnavailableError, OllamaEmbedder
from services.kb.retrieval.graph import build_class_hierarchy, glossary_recall
from services.kb.retrieval.retrieve import SearchHit
from services.platform.config import Settings
from services.platform.deps import Principal

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

TENANT = uuid.uuid4()
TARGET = "urn:k8:outage_order"
_DOC = uuid.uuid4()
_CHUNK = {
    "chunk_id": uuid.uuid4(),
    "document_id": _DOC,
    "content": "故障工单处理进度与归档要求说明。",
    "chunk_meta": {"span": [0, 16]},
    "doc_name": "运检手册v2",
    "minio_key": "raw-docs/t/d/source.md",
}


def _mini_catalog() -> SeedCatalog:
    """假目录（双签资产替身）：别名「报修单」/label「故障工单」→ target=urn:k8:outage_order。"""
    term = GlossaryTerm(iri="g:w1", label="故障工单", target=TARGET, aliases=("报修单",))
    index: dict[str, GlossaryTerm] = {}
    for name in (term.label, *term.aliases):
        index.setdefault(name.lower(), term)
    return SeedCatalog(
        classes=((TARGET, "故障工单", "OutageOrder"),),
        class_iris=frozenset({TARGET}),
        properties=(),
        shapes_graph=Graph(),
        glossary=(term,),
        glossary_alias_index=index,
    )


def _bare_catalog() -> SeedCatalog:
    """目录空（旧种子/外部目录档）：glossary=()。"""
    return SeedCatalog(classes=(), class_iris=frozenset(), properties=(), shapes_graph=Graph())


def _hierarchy() -> Any:
    """层次读模型（只含 target 类）：守卫口径=层次已知类（_all_class_keys）。"""
    return build_class_hierarchy([(TARGET, "故障工单", [])])


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def mappings(self) -> list[Any]:
        return self._rows


class _RecordingSession:
    """execute 捕获桩：按 SQL 片段路由预设行（层次读模型行=属性访问；业务行=dict 投影）。

    未预期 SQL 一律 AssertionError——测试对生产路径真实执行的语句集保持严格（多跑/漏跑即炸）。
    """

    def __init__(self, *, adjacency_rows: list[dict] | None = None, hierarchy_rows: list[Any] | None = None) -> None:
        self.adjacency_rows = adjacency_rows or []
        self.hierarchy_rows = hierarchy_rows or []
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, clause: object, params: dict | None = None) -> _Rows:
        sql = str(clause)
        self.calls.append((sql, dict(params or {})))
        if "FROM classes c" in sql:  # ontology.hierarchy_service 读模型查询
            return _Rows(self.hierarchy_rows)
        if "FROM kb_facts f" in sql:  # graph.glossary_recall 邻接查询（hop-0）
            return _Rows(self.adjacency_rows)
        if "websearch_to_tsquery" in sql:  # bm25 路（用例固定零词法命中，让第 4 路独占通道集）
            return _Rows([])
        raise AssertionError(f"桩会话未预期的 SQL: {sql}")

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _SessionFactory:
    """async_sessionmaker 替身：() → 异步上下文管理器（检索短会话即用即弃同形）。"""

    def __init__(self, session: _RecordingSession) -> None:
        self._session = session

    def __call__(self) -> _RecordingSession:
        return self._session


def _degraded_embedder() -> OllamaEmbedder:
    """注入式降级嵌入（api._embedder 走 app.state._kb_embedder 缓存位；嵌入必抛不触网）。"""
    embedder = OllamaEmbedder("http://localhost:9", timeout=0.1)

    async def _raise(texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("K8 接线用例：嵌入路注入降级")

    embedder.embed = _raise  # type: ignore[method-assign]
    return embedder


def _fake_request() -> SimpleNamespace:
    """/kb/search 路由签名所需最小 Request 替身（test_acl_search_api._fake_request 同构）。"""
    return SimpleNamespace(
        headers={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=Settings(kb_acl_filter_enabled=False),
                _kb_embedder=_degraded_embedder(),
                _kb_hierarchy_cache={},
            )
        ),
    )


def _principal() -> Principal:
    return Principal(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(TENANT),
            "roles": ["viewer"],
            "scopes": ["kb:read"],
            "typ": "access",
            "jti": f"test-{uuid.uuid4().hex[:8]}",
        }
    )


def _wired_session() -> _RecordingSession:
    return _RecordingSession(
        adjacency_rows=[dict(_CHUNK)],
        hierarchy_rows=[SimpleNamespace(iri=TARGET, name="故障工单", subclass_of=[])],
    )


# ── K8-a：glossary_recall 守卫 + hop-0 召回 ────────────────────────────────────


async def test_K8a_守卫_stale_target不进SQL_零查询返回空() -> None:
    """targets ∩ 层次已知类守卫：未知/stale target 全滤 → 空 list 且零 SQL（版本错位不触库）。"""
    session = _RecordingSession()
    hits = await glossary_recall(
        ["urn:k8:stale", ""], session=session, tenant_id=TENANT, hierarchy=_hierarchy(), limit=10
    )
    assert hits == []
    assert session.calls == []


async def test_K8a_守卫_空层次退化为空召回非失败() -> None:
    """无已发布本体读模型（空层次）→ 守卫全滤 → [] 非失败（OntRAG §4；第 4 路空命中不进通道集）。"""
    session = _RecordingSession()
    hits = await glossary_recall(
        [TARGET], session=session, tenant_id=TENANT, hierarchy=build_class_hierarchy([]), limit=5
    )
    assert hits == []
    assert session.calls == []


async def test_K8a_hop0召回_复用邻接SQL_守卫混滤_ACL与时点下推_结果同构() -> None:
    """hop-0 召回全断言：stale 混入滤除、复用 _ADJACENT_CHUNKS_SQL（同类权威 + support 序）、
    ACL 片段/参数与 as_of 下推（与图路逐跳同款谓词）、结果 SearchHit 同构投影。"""
    session = _RecordingSession(adjacency_rows=[dict(_CHUNK)])
    acl = AclPushdown()
    acl.fragment = " AND (d.acl_tags IS NULL OR d.acl_tags ?| CAST(:acl_tags AS text[]))"
    acl.params = {"acl_tags": ["dept:power"]}
    as_of = datetime(2026, 10, 1)
    hits = await glossary_recall(
        ["urn:k8:stale", TARGET],
        session=session,
        tenant_id=TENANT,
        hierarchy=_hierarchy(),
        limit=7,
        acl=acl,
        as_of=as_of,
    )
    assert len(session.calls) == 1
    sql, params = session.calls[0]
    assert "FROM kb_facts f" in sql  # hop-0 复用既有邻接 SQL（不 walk 层次闭包）
    assert "f.status = 'authoritative'" in sql  # 候选非成品不入检索（宪法 3）
    assert "max(f.confidence) AS support" in sql and "ORDER BY support DESC" in sql  # 先过滤后排序同款
    assert acl.fragment in sql and params["acl_tags"] == ["dept:power"]  # ACL 同源谓词下推
    assert params["tenant_id"] == TENANT
    assert params["classes"] == [TARGET]  # stale 混入被守卫滤除，仅合法 target 进 SQL
    assert params["cap"] == 7 and params["as_of"] == as_of  # bi-temporal 时点参数下推
    assert len(hits) == 1
    hit = hits[0]
    assert isinstance(hit, SearchHit)
    assert hit.chunk_id == _CHUNK["chunk_id"] and hit.document_id == _DOC
    assert hit.content == _CHUNK["content"] and hit.span == [0, 16]
    assert hit.doc_name == "运检手册v2" and hit.minio_key == _CHUNK["minio_key"]


# ── K8-b：共享 glossary_fn 构造器（search_service）─────────────────────────────


async def test_K8b_构造器_目录空返回None_零干扰() -> None:
    """目录空（旧种子/外部目录）→ None=hybrid_search 不启用第 4 路（零干扰，向后兼容红线）。"""
    session = _RecordingSession()
    fn = await build_glossary_recall_fn(
        session=session, tenant_id=TENANT, hierarchy=_hierarchy(), catalog=_bare_catalog()
    )
    assert fn is None
    assert session.calls == []


async def test_K8b_词面解析纯函数_别名label精确_同靶去重() -> None:
    """词面 → gloss:target：别名/label 归一精确命中（不做包含启发，agentic.rewrite 零级同款）；
    同靶多词面去重；无命中空表。"""
    catalog = _mini_catalog()
    assert glossary_targets_for_query("报修单 进度", catalog) == [TARGET]
    assert glossary_targets_for_query("故障工单", catalog) == [TARGET]  # label 面同入
    assert glossary_targets_for_query("故障工单 报修单", catalog) == [TARGET]  # 双词面同靶去重
    assert glossary_targets_for_query("完全无关词", catalog) == []


async def test_K8b_回调_无词面命中零SQL_有命中走glossary_recall() -> None:
    """回调链路：无词面命中 → [] 且零 SQL；有命中 → 词面→target→hop-0 召回（pool 即 limit）。"""
    session = _RecordingSession(adjacency_rows=[dict(_CHUNK)])
    fn = await build_glossary_recall_fn(
        session=session, tenant_id=TENANT, hierarchy=_hierarchy(), catalog=_mini_catalog()
    )
    assert fn is not None
    assert await fn("完全无关词", 50) == []
    assert session.calls == []
    hits = await fn("报修单 进度", 50)
    assert len(session.calls) == 1
    sql, params = session.calls[0]
    assert "FROM kb_facts f" in sql and params["classes"] == [TARGET] and params["cap"] == 50
    assert [h.chunk_id for h in hits] == [_CHUNK["chunk_id"]]


# ── K8-c：生产路径真可达（假目录经单例注入 → 第 4 路进融合）─────────────────────


async def test_K8c_服务调用点_第4路进RRF融合(monkeypatch: pytest.MonkeyPatch) -> None:
    """KnowledgeSearchService.search 生产调用点（search_service）：目录单例注入假目录 →
    bm25 零命中 + 嵌入降级下，glossary 单路进通道集，citations 带 gloss:target 召回 chunk。"""
    monkeypatch.setattr(agentic_mod, "_DEFAULT_CATALOG_CACHE", _mini_catalog())
    service = KnowledgeSearchService(
        _SessionFactory(_wired_session()),
        ollama_base_url="http://localhost:9",
        acl_filter_enabled=False,
        embed_protocol="ollama",
    )
    service._embedder = _degraded_embedder()  # type: ignore[assignment]  # 嵌入路注入降级（不触网）
    result = await service.search(tenant_id=TENANT, query="报修单 进度")
    assert "glossary" in result.channels and result.channels == ["glossary"]  # 第 4 路进融合
    assert [c.chunk_id for c in result.citations] == [_CHUNK["chunk_id"]]
    assert result.degraded and "vector_unavailable" in result.degraded_reasons  # 降级不吞第 4 路


async def test_K8c_api调用点_非agentic分支_第4路进RRF融合(monkeypatch: pytest.MonkeyPatch) -> None:
    """/kb/search 非 agentic 分支（api/kb.py else 臂）：假目录经 agentic 单例生效 →
    第 4 路进融合，hits/citations 全字段带 chunk。"""
    monkeypatch.setattr(agentic_mod, "_DEFAULT_CATALOG_CACHE", _mini_catalog())
    out = await kb_search_route(
        body=KbSearchIn(query="报修单 进度"),  # kb_id=None：埋点零动作
        principal=_principal(),
        request=_fake_request(),
        session=_wired_session(),
    )
    assert "glossary" in out.channels and out.channels == ["glossary"]
    assert out.hits[0].chunk_id == _CHUNK["chunk_id"]
    assert [c.chunk_id for c in out.citations] == [_CHUNK["chunk_id"]]
    assert [c.doc_name for c in out.citations] == ["运检手册v2"]


async def test_K8c_api调用点_agentic分支_第4路进RRF融合(monkeypatch: pytest.MonkeyPatch) -> None:
    """/kb/search agentic 分支（api/kb.py agentic_once 臂）：每轮闭包回调真可达——
    召回 chunk 带 span → 规则评级 pass 快速收敛，channels 含 glossary、trace 全链。"""
    monkeypatch.setattr(agentic_mod, "_DEFAULT_CATALOG_CACHE", _mini_catalog())
    out = await kb_search_route(
        body=KbSearchIn(query="报修单 进度", agentic=True, max_rounds=1),
        principal=_principal(),
        request=_fake_request(),
        session=_wired_session(),
    )
    assert "glossary" in out.channels and out.channels == ["glossary"]
    assert out.agentic is not None
    assert out.agentic.decision == "retrieval_required"
    assert out.agentic.rounds and out.agentic.rounds[0].grade == "pass"  # span 齐 + 归一分 1.0
    assert [c.chunk_id for c in out.citations] == [_CHUNK["chunk_id"]]
