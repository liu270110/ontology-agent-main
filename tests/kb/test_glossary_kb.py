# tests/kb/test_glossary_kb.py
"""K4 Glossary 业务术语层 kb 侧用例（docs/Agent/13 §9 K4-b/K4-c/K4-d；模型=tis@18 §10.1）。

断言目标：
- K4-b 实体链接：load_seed_catalog 术语目录/别名索引装载；match_seed_class 别名精确级
  （glossary_alias）命中返回 gloss:target，优先级高于包含；属性定位术语不参与类对齐；
- K4-c 改写换真目录：rewrite() 零级别名目录命中改写（含越过否定词表）、目录为空回原两级链
  （向后兼容）、规范术语面自命中不改写；
- K4-d 检索第 4 路：hybrid_search glossary 路进 RRF 融合（权重 0.5/0.4 对齐既有通道量级），
  空命中不进通道集、不干扰其余路（命中集/分数与不启用时一致）。
零外部依赖：真实种子资产装载 + 纯函数/回调桩（不触 DB/嵌入服务；不 import platform.db.registry
——该 import 面当前被 develop 既有集市批破损阻断，本文件刻意避开）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from rdflib import Graph

from services.kb.business.agentic import rewrite
from services.kb.business.kb_extraction import (
    GlossaryTerm,
    SeedCatalog,
    load_seed_catalog,
    match_seed_class,
)
from services.kb.retrieval.retrieve import (
    CHANNEL_WEIGHTS,
    CHANNEL_WEIGHTS_GRAPH,
    RRF_K,
    SearchHit,
    hybrid_search,
)

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

PW = "http://ontology-agent.local/o/t1/power#"


def _norm(name: str) -> str:
    """与 kb_extraction._norm 同形归一（去空白 + 小写；测试侧只读复刻，避免引私有符号）。"""
    import re

    return re.sub(r"\s+", "", name).lower()

# 迷你术语目录（synthetic 注入用）：别名/词表交互的确定性载体（真实种子目录另测）


def _mini_catalog_with_glossary() -> SeedCatalog:
    """双类 + 单术语条目：别名「报修单」→ target 类 B（与 contains 可达的类 A 不同靶）。"""
    alias_term = GlossaryTerm(iri="g:t1", label="故障工单", target="urn:test:b", aliases=("报修单",))
    blocker_term = GlossaryTerm(iri="g:t2", label="故障指示器", target="urn:test:b", aliases=("故障",))
    terms = (alias_term, blocker_term)
    index: dict[str, GlossaryTerm] = {}
    for term in terms:
        for name in (term.label, *term.aliases):
            index.setdefault(name, term)
    return SeedCatalog(
        classes=(("urn:test:a", "报修单模板", "Template"), ("urn:test:b", "工单B", "B")),
        class_iris=frozenset({"urn:test:a", "urn:test:b"}),
        properties=(),
        shapes_graph=Graph(),
        glossary=terms,
        glossary_alias_index=index,
    )


def _bare_catalog() -> SeedCatalog:
    """无术语条目的目录（目录为空=原两级链向后兼容档）。"""
    return SeedCatalog(
        classes=(("urn:test:transformer", "配电变压器", "Transformer"),),
        class_iris=frozenset({"urn:test:transformer"}),
        properties=(),
        shapes_graph=Graph(),
    )


def _hit(name: str, channels: list[str] | None = None) -> SearchHit:
    return SearchHit(
        chunk_id=uuid.uuid5(uuid.NAMESPACE_URL, name),
        document_id=uuid.uuid4(),
        content=f"chunk-{name}",
        channels=channels or [],
    )


# ── K4-b：术语目录装载 + 实体链接别名级 ─────────────────────────────────────────


def test_K4b_种子目录装载_术语与别名索引齐备() -> None:
    """真实种子：目录 ≥3 条、label+altLabel 全部入索引、target 指向种子类；gloss 元数据不入类/属性目录。"""
    catalog = load_seed_catalog()
    assert len(catalog.glossary) >= 3, f"术语目录不足 3 条: {len(catalog.glossary)}"
    for term in catalog.glossary:
        assert term.target in catalog.class_iris, f"{term.label} 的 target 不是种子类: {term.target}"
        assert _norm(term.label) in catalog.glossary_alias_index
        for alias in term.aliases:
            assert _norm(alias) in catalog.glossary_alias_index, f"{term.label} 别名缺失: {alias}"
    # gloss 元类不污染抽取目录（提示词引导/对齐白名单面）
    gloss_ns = "https://ontology-agent.dev/ns/gloss#"
    assert all(not iri.startswith(gloss_ns) for iri, _, _ in catalog.classes)
    assert all(not iri.startswith(gloss_ns) for iri, _, _ in catalog.properties)


def test_K4b_别名链接命中_返回target与glossary_alias规则() -> None:
    """别名（报修单/停户数）与术语 label（停电户数）精确命中 → 返回 (gloss:target, "glossary_alias")。"""
    catalog = load_seed_catalog()
    assert match_seed_class("报修单", catalog) == (f"{PW}OutageOrder", "glossary_alias")
    assert match_seed_class("停户数", catalog) == (f"{PW}Customer", "glossary_alias")
    assert match_seed_class("停电户数", catalog) == (f"{PW}Customer", "glossary_alias")  # label 亦入目录


def test_K4b_别名级优先于包含匹配() -> None:
    """「报修单」字面包含于类标签「报修单模板」（contains 可达 A）——别名级必须先命中并返回目录靶 B。"""
    catalog = _mini_catalog_with_glossary()
    assert match_seed_class("报修单", catalog) == ("urn:test:b", "glossary_alias")


def test_K4b_属性定位术语不参与类对齐() -> None:
    """gloss:target 指向属性（不在 class_iris）→ 别名级跳过返回 None（防 ABox 误类型化）。"""
    prop_term = GlossaryTerm(iri="g:p1", label="工单编号", target=f"{PW}orderNo", aliases=())
    catalog = SeedCatalog(
        classes=(("urn:test:a", "报修单模板", "Template"),),
        class_iris=frozenset({"urn:test:a"}),
        properties=((f"{PW}orderNo", "工单编号", "orderNo"),),
        shapes_graph=Graph(),
        glossary=(prop_term,),
        glossary_alias_index={"工单编号": prop_term},
    )
    assert match_seed_class("工单编号", catalog) is None


def test_K4b_既有精确与包含链不受影响_回归() -> None:
    """别名级插入后，类标签 exact（变压器）与 contains（变压）两级行为不变。"""
    catalog = load_seed_catalog()
    assert match_seed_class("变压器", catalog) == (f"{PW}Transformer", "exact")
    assert match_seed_class("变压", catalog) == (f"{PW}Transformer", "contains")
    assert match_seed_class("完全无关词", catalog) is None


# ── K4-c：改写换真目录 ─────────────────────────────────────────────────────────


async def test_K4c_rewrite_换真目录_别名命中改写() -> None:
    """别名目录命中 → 归一为规范术语标签（原词表/启发式不可达的「报修单」被目录收编）。"""
    catalog = load_seed_catalog()
    assert await rewrite("报修单 进度", catalog=catalog) == ("故障工单 进度", "term_alias:故障工单")
    assert await rewrite("停户数 统计", catalog=catalog) == ("停电户数 统计", "term_alias:停电户数")


async def test_K4c_rewrite_别名级优先于否定词表() -> None:
    """「故障」在否定词表——但目录别名命中是双签资产，零级先于词表（aliases 取代词表的既定方向）。"""
    assert await rewrite("故障 停电", catalog=_mini_catalog_with_glossary()) == (
        "故障指示器 停电",
        "term_alias:故障指示器",
    )


async def test_K4c_rewrite_目录为空_回原两级链向后兼容() -> None:
    """目录空=无别名可查 → 行为与原词表档逐字一致（二级按序包含仍可达；不可归一返回 None）。"""
    bare = _bare_catalog()
    assert await rewrite("配变 停电原因", catalog=bare) == ("配电变压器 停电原因", "term_alias:配电变压器")
    assert await rewrite("完全无关词", catalog=bare) is None


async def test_K4c_rewrite_真实种子_词表兜底与规范面自命中不改写() -> None:
    """真实种子回归：规范术语/词表泛词不改写；术语 label 自命中（停电范围）不改写。"""
    catalog = load_seed_catalog()
    assert await rewrite("变压器 故障", catalog=catalog) is None  # exact 自命中 + 泛词词表兜底
    assert await rewrite("停电范围", catalog=catalog) is None  # 术语 label 自命中 → 该段无需归一
    assert await rewrite("馈线F001停电", catalog=catalog) is None  # 标签⊂查询词守卫不变


# ── K4-d：检索第 4 路（glossary 进 RRF）────────────────────────────────────────


def _two_channel_stubs():
    ids = {
        name: uuid.uuid5(uuid.NAMESPACE_URL, name) for name in ("A", "B", "C")
    }
    doc = uuid.uuid4()

    def hit(name: str) -> SearchHit:
        return SearchHit(chunk_id=ids[name], document_id=doc, content=f"chunk-{name}")

    async def bm25_fn(q: str, k: int) -> list[SearchHit]:
        return [hit("A"), hit("B")]

    async def vector_fn(q: str, k: int) -> list[SearchHit]:
        return [hit("C"), hit("A")]

    return ids, bm25_fn, vector_fn


async def test_K4d_hybrid_第4路命中_进RRF融合() -> None:
    """glossary 路命中参与融合：权重 0.5（两路表量级对齐）、channels 并集含 glossary、D 分=0.5/(k+1)。"""
    ids, bm25_fn, vector_fn = _two_channel_stubs()
    d_id = uuid.uuid5(uuid.NAMESPACE_URL, "D")
    doc = uuid.uuid4()

    async def glossary_fn(q: str, k: int) -> list[SearchHit]:
        return [SearchHit(chunk_id=d_id, document_id=doc, content="chunk-D")]

    result = await hybrid_search("报修单", bm25=bm25_fn, vector=vector_fn, glossary=glossary_fn, top_k=8)
    assert "glossary" in result.channels and set(result.channels) == {"bm25", "vector", "glossary"}
    d_hit = next(h for h in result.hits if h.chunk_id == d_id)
    assert d_hit.channels == ["glossary"]
    assert d_hit.score == CHANNEL_WEIGHTS["glossary"] / (RRF_K + 1)  # 单路 rank1
    # 术语精确命中把 D 抬进结果集（bm25=[A,B] + vector=[C,A] 并集 3 块 → 加 D 共 4 块）
    assert len(result.hits) == 4


async def test_K4d_hybrid_glossary空命中_不干扰其余路() -> None:
    """glossary 回调返回空 → 通道集不含 glossary，命中集/分数与不启用该路时逐块一致。"""
    ids, bm25_fn, vector_fn = _two_channel_stubs()

    async def glossary_empty(q: str, k: int) -> list[SearchHit]:
        return []

    with_glossary = await hybrid_search("报修单", bm25=bm25_fn, vector=vector_fn, glossary=glossary_empty, top_k=8)
    without = await hybrid_search("报修单", bm25=bm25_fn, vector=vector_fn, top_k=8)
    assert "glossary" not in with_glossary.channels and with_glossary.channels == without.channels
    assert [h.chunk_id for h in with_glossary.hits] == [h.chunk_id for h in without.hits]
    assert all(a.score == b.score for a, b in zip(with_glossary.hits, without.hits, strict=True))


def test_K4d_权重表_量级对齐既有通道() -> None:
    """glossary 权重入场：两路表 0.5（对齐 bm25/vector）、含图表 0.4（对齐 bm25/vector 档）。"""
    assert CHANNEL_WEIGHTS["glossary"] == CHANNEL_WEIGHTS["bm25"] == CHANNEL_WEIGHTS["vector"]
    assert CHANNEL_WEIGHTS_GRAPH["glossary"] == CHANNEL_WEIGHTS_GRAPH["bm25"] == CHANNEL_WEIGHTS_GRAPH["vector"]
