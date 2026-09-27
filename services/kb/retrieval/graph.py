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
