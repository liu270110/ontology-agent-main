"""LazyGraphRAG lite 图路（OntRAG §4.0 默认档：查询时图遍历，零 Neo4j、零预建社区）。

- 锚定：bm25/vector 命中 chunk → 关联权威事实（kb_facts.status='authoritative'，底线 1
  候选非成品：candidate 不参与检索）的类 IRI（subject_type + aliases 中的类 IRI，align 步写入）；
- 邻接扩展：类闭包（本体层次 self∪ancestors∪descendants）→ 同类其他 chunks，
  max_hops 控制跳数；PG 查询走本模块 raw SQL（embed.py 同款模式），
  先过滤后排序（§4.3：租户/集合/valid_to ACL 硬过滤下推 SQL，候选集 GRAPH_CANDIDATE_CAP 上限护栏）；
- 图路表示：类 IRI 链（节点=类、边=subclass_of/same_class）进 evidence.graph_paths（§5 lite 形态）；
- 类层次来源：ontology 模块公开查询函数（services/ontology/api，standards/01 §2.1 规则 3
  显式服务调用面；kb 禁入 ontology.data），kb 侧进程内缓存（api 层 app.state，TTL 300s 示例值）；
  无已发布本体读模型时闭包=类自身，同类扩展照常可用（层次扩展为增量增强，非必需依赖）。
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.kb.retrieval.embed import AclPushdown, _acl_filter
from services.kb.retrieval.retrieve import GraphExpansion, GraphNode, GraphPath, GraphRel, SearchHit

GRAPH_CANDIDATE_CAP = 100  # §4.3 图遍历护栏 lite 档：候选集硬上限（示例值/待实测）
GRAPH_HOP_DECAY = 0.5  # same_class 边权逐跳衰减底数（信息值，示例值）
_PATH_NODE_CAP = 32  # 单图路节点上限（防超大连通片撑爆响应）
_PATH_REL_CAP = 48
_FACT_BATCH = 64  # 种子/扩展 chunk 的事实查询分批大小

# 种子/扩展 chunk 的权威事实类集合（subject_type + aliases 中的类 IRI）
_FACTS_OF_CHUNKS_SQL = (
    "SELECT f.chunk_id, f.subject_type, f.aliases "
    "FROM kb_facts f "
    "WHERE f.tenant_id = :tenant_id AND f.status = 'authoritative' "
    "AND f.chunk_id = ANY(CAST(:chunk_ids AS uuid[]))"
)

# 类闭包 → 邻接 chunks（§4.3 先过滤后排序：ACL 硬过滤在排序前下推；support=最高事实置信度）
_ADJACENT_CHUNKS_SQL = (  # {acl} = embed._acl_filter（§4.3 + §8.2 同源谓词）
    "SELECT c.id AS chunk_id, c.document_id, c.content, c.meta AS chunk_meta, "
    "d.title AS doc_name, d.minio_key, max(f.confidence) AS support "
    "FROM kb_facts f "
    "JOIN document_chunks c ON c.id = f.chunk_id "
    "JOIN documents d ON d.id = c.document_id "
    "WHERE f.tenant_id = :tenant_id AND f.status = 'authoritative' AND f.chunk_id IS NOT NULL "
    "AND {acl} "
    "AND (f.subject_type = ANY(CAST(:classes AS text[])) OR f.aliases ?| CAST(:classes AS text[])) "
    "GROUP BY c.id, c.document_id, c.content, c.meta, d.title, d.minio_key "
    "ORDER BY support DESC, c.id "
    "LIMIT :cap"
)


@dataclass(frozen=True, slots=True)
class ClassHierarchy:
    """本体类层次只读视图（iri → 名称/直接父类/直接子类；来自 ontology 读模型公开查询）。"""

    names: Mapping[str, str] = field(default_factory=dict)
    parents: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    children: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def _walk(self, roots: Iterable[str], edges: Mapping[str, Sequence[str]]) -> set[str]:
        seen: set[str] = set()
        stack = [iri for iri in roots if iri]
        while stack:
            iri = stack.pop()
            if iri in seen:
                continue
            seen.add(iri)
            stack.extend(e for e in edges.get(iri, ()) if e not in seen)
        return seen

    def closure(self, iris: Iterable[str]) -> set[str]:
        """扩展闭包：self ∪ ancestors ∪ descendants（同类邻接的层次语义：父类相通即同类）。"""
        roots = {iri for iri in iris if iri}
        if not roots:
            return set()
        return roots | self._walk(roots, self.parents) | self._walk(roots, self.children)

    def filter_closure(self, iris: Iterable[str]) -> set[str]:
        """过滤闭包：self ∪ descendants（entity_type_filter 语义：过滤父类蕴含其子类）。"""
        roots = {iri for iri in iris if iri}
        if not roots:
            return set()
        return roots | self._walk(roots, self.children)


def build_class_hierarchy(rows: Iterable[tuple[str, str, Sequence[str]]]) -> ClassHierarchy:
    """ontology 公开查询行 (iri, name, subclass_of) → 只读层次视图（双向邻接表）。"""
    names: dict[str, str] = {}
    parents: dict[str, list[str]] = {}
    children: dict[str, list[str]] = {}
    for iri, name, supers in rows:
        if not iri:
            continue
        names.setdefault(iri, name)
        parents.setdefault(iri, [])
        children.setdefault(iri, [])
        for parent in supers or ():
            if not parent:
                continue
            parents[iri].append(parent)
            children.setdefault(parent, []).append(iri)
    return ClassHierarchy(
        names={k: v for k, v in names.items()},
        parents={k: tuple(dict.fromkeys(v)) for k, v in parents.items()},
        children={k: tuple(dict.fromkeys(v)) for k, v in children.items()},
    )


def _local_name(iri: str) -> str:
    """IRI 本地名兜底（ontology 读模型未覆盖的类，如平台顶类 ob2:*）。"""
    for sep in ("#", "/"):
        if sep in iri:
            iri = iri.rsplit(sep, 1)[-1]
    return iri


def _fact_classes(row: Mapping[str, object]) -> set[str]:
    """单事实 → 类 IRI 集合（subject_type + aliases 字符串项）。"""
    out: set[str] = set()
    subject_type = row["subject_type"]
    if isinstance(subject_type, str) and subject_type:
        out.add(subject_type)
    aliases = row["aliases"]
    if isinstance(aliases, list):
        out.update(a for a in aliases if isinstance(a, str) and a)
    return out


def _row_to_hit(row: Mapping[str, object]) -> SearchHit:
    meta = row["chunk_meta"] or {}
    span = meta.get("span") if isinstance(meta, dict) else None
    return SearchHit(
        chunk_id=row["chunk_id"],  # type: ignore[arg-type]
        document_id=row["document_id"],  # type: ignore[arg-type]
        content=str(row["content"]),
        doc_name=row["doc_name"] if isinstance(row["doc_name"], str) else None,
        minio_key=row["minio_key"] if isinstance(row["minio_key"], str) else None,
        span=span if isinstance(span, list) else None,
    )


async def _classes_of_chunks(
    session: AsyncSession, *, tenant_id: uuid.UUID, chunk_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, set[str]]:
    """chunk → 权威事实类集合（分批 IN 查询；仅命中到 chunk_id 非空的行）。"""
    out: dict[uuid.UUID, set[str]] = {}
    for start in range(0, len(chunk_ids), _FACT_BATCH):
        batch = list(chunk_ids[start : start + _FACT_BATCH])
        rows = await session.execute(text(_FACTS_OF_CHUNKS_SQL), {"tenant_id": tenant_id, "chunk_ids": batch})
        for row in rows.mappings():
            if row["chunk_id"] is None:
                continue
            out.setdefault(row["chunk_id"], set()).update(_fact_classes(row))
    return out


def _node(iri: str, hierarchy: ClassHierarchy, members: set[str]) -> GraphNode:
    """图路节点：类 IRI + 展示名（读模型名 → 本地名兜底）+ 类型（路径内首个父类）。"""
    parent = next((p for p in hierarchy.parents.get(iri, ()) if p in members), None)
    return GraphNode(iri=iri, name=hierarchy.names.get(iri) or _local_name(iri), type=parent)


def _class_chain(
    discovered: Sequence[str],
    chunk_ids: Sequence[uuid.UUID],
    hierarchy: ClassHierarchy,
    hops_used: Sequence[int],
) -> GraphPath | None:
    """聚合图路（§5 lite 形态）：类 IRI 链 = 种子类（hop 0）+ 逐跳扩展类。

    边：subclass_of（本体层次，路径内父子都可见才成边）+ same_class（每跳一条，权重逐跳衰减）；
    chunk_ids = 锚定种子 + 扩展端点（供引用追溯）。
    """
    nodes_iris = list(dict.fromkeys(discovered))[:_PATH_NODE_CAP]
    if not nodes_iris:
        return None
    members = set(nodes_iris)
    rels = [
        GraphRel(type="subclass_of", weight=1.0)
        for iri in nodes_iris
        for parent in hierarchy.parents.get(iri, ())
        if parent in members
    ]
    rels.extend(GraphRel(type="same_class", weight=GRAPH_HOP_DECAY**hop) for hop in hops_used)
    return GraphPath(
        nodes=[_node(iri, hierarchy, members) for iri in nodes_iris],
        rels=rels[:_PATH_REL_CAP],
        chunk_ids=list(chunk_ids),
    )


async def expand_graph(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    collection_id: uuid.UUID | None = None,
    seeds: Sequence[SearchHit],
    hierarchy: ClassHierarchy,
    max_hops: int = 2,
    entity_type_filter: Sequence[str] | None = None,
    candidate_cap: int = GRAPH_CANDIDATE_CAP,
    as_of: datetime | None = None,
    include_superseded: bool = False,
    acl: AclPushdown | None = None,
) -> GraphExpansion:
    """查询时图遍历（LazyGraphRAG lite）：种子 chunks → 权威类 IRI → 类闭包邻接扩展（max_hops）。

    返回图通道命中（BFS 序：逐跳、跳内 support 降序）+ 类 IRI 链图路 + entity 过滤匹配集；
    entity_type_filter 给出时：种子/扩展 chunk 均须命中过滤闭包（self∪descendants）才入匹配集；
    acl（AclPushdown 激活时）与 §4.3 同源谓词一并先过滤后排序下推邻接查询。
    """
    seed_ids: list[uuid.UUID] = []
    for hit in seeds:
        if hit.chunk_id not in seed_ids:
            seed_ids.append(hit.chunk_id)
    if not seed_ids:
        return GraphExpansion()

    allowed = hierarchy.filter_closure(entity_type_filter) if entity_type_filter else None
    classes_by_chunk = await _classes_of_chunks(session, tenant_id=tenant_id, chunk_ids=seed_ids)
    if allowed is not None:
        classes_by_chunk = {cid: cls & allowed for cid, cls in classes_by_chunk.items() if cls & allowed}
    matched = set(classes_by_chunk)
    anchor_classes = {cls for s in classes_by_chunk.values() for cls in s}

    visited: set[uuid.UUID] = set(seed_ids)
    expanded_hits: list[SearchHit] = []
    discovered: list[str] = sorted(anchor_classes)  # 类 IRI 链：种子类（hop 0）在前
    seen_classes: set[str] = set(discovered)
    current = set(anchor_classes)
    hops_used: list[int] = []
    for hop in range(1, max_hops + 1):
        if not current or len(expanded_hits) >= candidate_cap:
            break
        closure = hierarchy.closure(current)
        acl_sql = _acl_filter(as_of, include_superseded) + (acl.fragment if acl is not None else "")
        rows = await session.execute(
            text(_ADJACENT_CHUNKS_SQL.format(acl=acl_sql)),
            {
                "tenant_id": tenant_id,
                "collection_id": collection_id,
                "classes": sorted(closure),
                "cap": candidate_cap - len(expanded_hits),
                "as_of": as_of,
                **(acl.params if acl is not None else {}),
            },
        )
        candidates = [row for row in rows.mappings() if row["chunk_id"] not in visited]
        for row in candidates:  # 已见 chunk 不再回访（防环）
            visited.add(row["chunk_id"])
        if not candidates:
            break
        hop_classes = await _classes_of_chunks(
            session, tenant_id=tenant_id, chunk_ids=[row["chunk_id"] for row in candidates]
        )
        if allowed is not None:  # 过滤语义：扩展端点同样必须命中过滤闭包
            hop_classes = {cid: cls & allowed for cid, cls in hop_classes.items() if cls & allowed}
        row_by_id = {row["chunk_id"]: row for row in candidates}
        kept_ids = [cid for cid in (row["chunk_id"] for row in candidates) if hop_classes.get(cid)]
        if not kept_ids:
            break
        expanded_hits.extend(_row_to_hit(row_by_id[cid]) for cid in kept_ids)
        for cid in kept_ids:
            matched.add(cid)
            classes_by_chunk.setdefault(cid, set()).update(hop_classes[cid])
        new_classes = {cls for cid in kept_ids for cls in hop_classes[cid]} - seen_classes
        seen_classes |= new_classes
        discovered.extend(sorted(new_classes))
        hops_used.append(hop)
        current = new_classes
    path = _class_chain(discovered, [*seed_ids, *(hit.chunk_id for hit in expanded_hits)], hierarchy, hops_used)
    return GraphExpansion(
        hits=expanded_hits,
        paths=[path] if path is not None else [],
        matched_chunk_ids=matched,
        classes=sorted(seen_classes),
    )


# ---------------------------------------------------------------- glossary 第 4 路 hop-0 召回（K8-a，13 §14）


async def glossary_recall(
    targets: Iterable[str],
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    hierarchy: ClassHierarchy,
    limit: int,
    acl: AclPushdown | None = None,
    as_of: datetime | None = None,
    include_superseded: bool = False,
) -> list[SearchHit]:
    """glossary 第 4 路 hop-0 召回（K8-a）：gloss:target 类 IRI → 同类权威 chunk 精确命中集。

    targets=目录命中的 gloss:target 类 IRI 集合（调用方经 search_service.build_glossary_recall_fn
    从查询词面解析）；守卫=∩层次读模型已知类（_all_class_keys，与图三查 _match_classes 同源
    口径）——目录与检索视图版本错位的 stale target 不进 SQL，空集/空层次（无已发布本体）零查询
    返回 []（第 4 路空命中不进通道集，K4-d 约束；OntRAG §4 空结果非失败）。查询即
    _ADJACENT_CHUNKS_SQL 的 hop-0 版（不 walk 层次闭包=0 跳语义；谓词与 expand_graph 逐跳
    同款：§4.3 ACL + §8.2 bi-temporal 先过滤后排序下推），参数形态对齐既有路；结果与
    bm25/vector 同构（list[SearchHit]，support 为信息值不入 hit——RRF 按路内名次计分）。
    """
    classes = sorted({t for t in targets if t} & set(_all_class_keys(hierarchy)))
    if not classes:
        return []
    acl_sql = _acl_filter(as_of, include_superseded) + (acl.fragment if acl is not None else "")
    rows = await session.execute(
        text(_ADJACENT_CHUNKS_SQL.format(acl=acl_sql)),
        {
            "tenant_id": tenant_id,
            "classes": classes,
            "cap": max(limit, 0),
            "as_of": as_of,
            **(acl.params if acl is not None else {}),
        },
    )
    return [_row_to_hit(row) for row in rows.mappings()]


# ---------------------------------------------------------------- 类级图三查（api/01 §5.4 kb 行 /kb/graph/*）
#
# lite=类级图（与 expand_graph 同源的层次读模型）：节点=本体类（ontology 读模型公开查询），
# 边=subclass_of（层次）+ 关系谓词（kb_facts authoritative relation——候选不入图=宪法第 3 条，
# 墓碑文档（documents.valid_to 封口）的关系边一并下线）。三查均为纯函数（层次+关系边入参，
# 零会话），会话面仅 authoritative_relations 一条只读 SQL；空结果非失败（OntRAG §4）。

QUERY_NODE_CAP = 64  # 图三查节点上限（防全租户图撑爆响应；种子/关系对端恒保留不计入截断，示例值/待实测）
QUERY_REL_CAP = 96  # 图三查边上限（同上；subclass_of 与关系边交错预算防挤占）
RELATION_EDGE_CAP = 200  # 权威关系边单次装载上限（lite 护栏；示例值/待实测）
RELATION_ATTACH_CAP = 32  # 单次图查询实际挂载的关系边上限（防关系对端类无限膨胀节点集；示例值/待实测）

# 权威关系边源（仅 authoritative + fact_type=relation；墓碑文档 join 封口下线；
# DISTINCT 去重——同三元组被多文档/chunk 断言只出一条边；predicate 非空防呆纵深——
# 空谓词行不得入图（api 层 DTO type 必填，缺防呆会 500））
_RELATION_EDGES_SQL = (
    "SELECT DISTINCT f.subject_type, f.predicate, f.object_type "
    "FROM kb_facts f "
    "JOIN documents d ON d.id = f.document_id AND d.valid_to IS NULL "
    "WHERE f.tenant_id = :tenant_id AND f.status = 'authoritative' AND f.fact_type = 'relation' "
    "AND f.subject_type IS NOT NULL AND f.predicate IS NOT NULL AND f.object_type IS NOT NULL "
    "ORDER BY f.subject_type, f.predicate, f.object_type "
    "LIMIT :cap"
)


@dataclass(slots=True)
class GraphQueryResult:
    """类级图三查结果：节点 + 边列表（matched=查询命中的种子类 IRI，供调用方回显）。"""

    nodes: list[GraphNode] = field(default_factory=list)
    rels: list[GraphRel] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)


async def authoritative_relations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    relation_cap: int = RELATION_EDGE_CAP,
) -> list[tuple[str, str, str]]:
    """租户权威关系边 (subject_type, predicate, object_type)——图三查的关系扩展源。

    仅 authoritative（候选非成品不入图）；墓碑文档（valid_to 封口）的关系边一并下线。
    """
    rows = await session.execute(text(_RELATION_EDGES_SQL), {"tenant_id": tenant_id, "cap": relation_cap})
    return [(row[0], row[1], row[2]) for row in rows]


def _known_class(hierarchy: ClassHierarchy, iri: str) -> bool:
    """类 IRI 是否在层次读模型中（names/parents/children 任一出现即视为已知类）。"""
    return iri in hierarchy.names or iri in hierarchy.parents or iri in hierarchy.children


def _all_class_keys(hierarchy: ClassHierarchy) -> list[str]:
    """类键全集（names ∪ parents ∪ children，稳定序）：层次读模型未作行的纯超类键
    （如平台顶类 ob2:*）与已行类同权可见——search 面与 neighborhood/path 面口径一致。"""
    return sorted(set(hierarchy.names) | set(hierarchy.parents) | set(hierarchy.children))


def _match_classes(hierarchy: ClassHierarchy, needle: str) -> list[str]:
    """关键词/IRI 片段匹配类（大小写不敏感：IRI 全串 / 读模型名 / 本地名三面），稳定序。

    扫描类键全集（_all_class_keys，含 parents/children 里的纯超类键）——保证 search 命中的
    类必被 _known_class 认可，两处口径永不漂移。
    """
    low = needle.strip().lower()
    if not low:
        return []
    return [
        iri
        for iri in _all_class_keys(hierarchy)
        if low in iri.lower() or low in (hierarchy.names.get(iri) or "").lower() or low in _local_name(iri).lower()
    ]


def _hierarchy_bfs(hierarchy: ClassHierarchy, seeds: set[str], depth: int) -> set[str]:
    """层次双向 BFS（父+子边同权行走）depth 跳内的类集合（含种子）。"""
    members = set(seeds)
    frontier = set(seeds)
    for _ in range(max(0, depth)):
        nxt: set[str] = set()
        for iri in frontier:
            nxt.update(hierarchy.parents.get(iri, ()))
            nxt.update(hierarchy.children.get(iri, ()))
        nxt -= members
        if not nxt:
            break
        members |= nxt
        frontier = nxt
        if len(members) >= QUERY_NODE_CAP:
            break
    return members


def _subclass_edges(hierarchy: ClassHierarchy, members: set[str]) -> list[GraphRel]:
    """节点集内 subclass_of 边（父子均在集内才成边）。"""
    return [
        GraphRel(type="subclass_of", weight=1.0)
        for iri in sorted(members)
        for parent in hierarchy.parents.get(iri, ())
        if parent in members
    ]


def _relation_edges(
    relations: Sequence[tuple[str, str, str]],
    members: set[str],
    *,
    relation_type: str | None = None,
    attach_cap: int = RELATION_ATTACH_CAP,
) -> list[tuple[str, str, str]]:
    """触及节点集的权威关系边（relation_type 给定时按谓词精确过滤）；对端类随边并入节点集。

    attach_cap 截断挂载边数（输入已稳定序，截断确定）——防大量关系对端类把节点集撑穿
    QUERY_NODE_CAP；被截断的边整体缺席（不带悬挂端点），节点/边引用一致性不破。
    """
    edges = [
        (s, p, o)
        for s, p, o in relations
        if (s in members or o in members) and (relation_type is None or p == relation_type)
    ]
    edges = edges[:attach_cap]
    for s, _, o in edges:
        members.add(s)
        members.add(o)
    return edges


def _select_nodes(members: set[str], preserved: set[str], cap: int) -> list[str]:
    """节点截断（ocr 评审 high/medium）：preserved（种子 + 关系对端类）恒保留，cap 只裁纯
    扩展层——matched 与关系边引用的 IRI 不得在 nodes 中缺席；整体稳定序。"""
    keep = sorted(preserved & members)
    extension = sorted(m for m in members if m not in preserved)
    return [*keep, *extension[: max(cap - len(keep), 0)]]


def _merge_edges(subclass_rels: list[GraphRel], relation_rels: list[GraphRel], cap: int) -> list[GraphRel]:
    """边预算（ocr 评审 low）：subclass_of 与关系边交错选取，cap 压力下单类不挤光另一类；
    任一类耗尽后其预算自然让渡给另一类；结果确定（输入各自稳定序）。"""
    out: list[GraphRel] = []
    si = ri = 0
    while len(out) < cap and (si < len(subclass_rels) or ri < len(relation_rels)):
        if si < len(subclass_rels):
            out.append(subclass_rels[si])
            si += 1
        if len(out) < cap and ri < len(relation_rels):
            out.append(relation_rels[ri])
            ri += 1
    return out


def graph_search(
    hierarchy: ClassHierarchy,
    query: str,
    *,
    depth: int = 1,
    top_k: int = 10,
    relations: Sequence[tuple[str, str, str]] = (),
) -> GraphQueryResult:
    """图检索（api/01 §5.4 GET /kb/graph/search）：q 关键词/IRI 片段匹配类 → 层次 depth 跳扩展
    + 权威关系边扩展（对端类并入节点集，类层次+关系扩展语义）。

    无匹配类 → 空结果（非失败，OntRAG §4）；top_k 截断匹配种子；节点截断恒保留种子与
    关系对端类（_select_nodes），边集 subclass_of/关系边交错预算（_merge_edges）。
    """
    seeds = _match_classes(hierarchy, query)[:top_k]
    if not seeds:
        return GraphQueryResult()
    members = _hierarchy_bfs(hierarchy, set(seeds), depth)
    rel_edges = _relation_edges(relations, members)
    preserved = set(seeds) | {s for s, _, _ in rel_edges} | {o for _, _, o in rel_edges}
    nodes = _select_nodes(members, preserved, QUERY_NODE_CAP)
    member_set = set(nodes)
    rels = _merge_edges(
        _subclass_edges(hierarchy, member_set),
        [GraphRel(type=predicate, weight=1.0) for s, predicate, o in rel_edges if s in member_set and o in member_set],
        QUERY_REL_CAP,
    )
    return GraphQueryResult(
        nodes=[_node(iri, hierarchy, member_set) for iri in nodes],
        rels=rels,
        matched=seeds,
    )


def graph_neighborhood(
    hierarchy: ClassHierarchy,
    class_iri: str,
    *,
    depth: int = 1,
    limit: int = 20,
    relations: Sequence[tuple[str, str, str]] = (),
    relation_type: str | None = None,
) -> GraphQueryResult:
    """邻域查询（api/01 §5.4 GET /kb/graph/neighborhood）：从类 IRI 出发层次近邻 depth 跳
    （lite 语义一跳直达，上限 2）+ 触及该类的权威关系边（谓词可过滤）。

    被查类与关系对端类恒入 nodes（limit 只裁纯扩展层，matched 不得列出缺席 IRI）；
    未知类 IRI（不在层次读模型）→ 空结果非失败（OntRAG §4，404 仅用于资源不存在口径）。
    """
    iri = class_iri.strip()
    if not iri or not _known_class(hierarchy, iri):
        return GraphQueryResult()
    members = _hierarchy_bfs(hierarchy, {iri}, depth)
    rel_edges = _relation_edges(relations, members, relation_type=relation_type)
    preserved = {iri} | {s for s, _, _ in rel_edges} | {o for _, _, o in rel_edges}
    nodes = _select_nodes(members, preserved, limit)
    member_set = set(nodes)
    rels = _merge_edges(
        _subclass_edges(hierarchy, member_set),
        [GraphRel(type=predicate, weight=1.0) for s, predicate, o in rel_edges if s in member_set and o in member_set],
        QUERY_REL_CAP,
    )
    return GraphQueryResult(
        nodes=[_node(x, hierarchy, member_set) for x in nodes],
        rels=rels,
        matched=[iri],
    )


def graph_shortest_path(
    hierarchy: ClassHierarchy,
    from_iri: str,
    to_iri: str,
    *,
    max_hops: int = 3,
) -> GraphPath | None:
    """路径查询（api/01 §5.4 GET /kb/graph/path）：类层次图内 BFS 最短路（父子边双向可行走——
    subclass_of 链上下行语义均有效），max_hops 限深；无路/端点未知 → None（空结果非失败）。
    """
    src, dst = from_iri.strip(), to_iri.strip()
    if not src or not dst or not _known_class(hierarchy, src) or not _known_class(hierarchy, dst):
        return None
    depth_of: dict[str, int] = {src: 0}
    prev: dict[str, str | None] = {src: None}
    queue: deque[str] = deque([src])
    while queue:
        cur = queue.popleft()
        if cur == dst:
            break
        cur_depth = depth_of[cur]
        if cur_depth >= max_hops:
            continue
        for nxt in (*hierarchy.parents.get(cur, ()), *hierarchy.children.get(cur, ())):
            if nxt not in depth_of:
                depth_of[nxt] = cur_depth + 1
                prev[nxt] = cur
                queue.append(nxt)
    if dst not in depth_of:
        return None
    chain = [dst]
    while chain[-1] != src:
        chain.append(prev[chain[-1]])  # type: ignore[arg-type]
    chain.reverse()
    members = set(chain)
    return GraphPath(
        nodes=[_node(iri, hierarchy, members) for iri in chain],
        rels=[GraphRel(type="subclass_of", weight=1.0) for _ in chain[1:]],
        chunk_ids=[],
    )
