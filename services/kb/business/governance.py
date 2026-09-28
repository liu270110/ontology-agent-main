"""知识治理主干 v1：冲突分诊 + 文档版本承接判定 + 落库（OntRAG 知识库GraphRAG设计 §8.0/§8.1/§8.2 lite 落位）。

职责与口径：
- detect_conflicts（§8.1 冲突分诊）：候选 × 既有权威事实，比对键 = subject + predicate，
  固定顺序 T1→T4→T3→T2（先廉价确定性判定、后语义判定）；纯函数不触库。
  T1 版本演进：lite 判定 = document_id 同源（同文档重抽，新版事实遮蔽旧版）→ 建 superseded_by
  边，非冲突无需人工；跨文档版本链（documents.identity_key 链）lite 未接，此类命中降级走
  T4/T3/T2（§8.0 三级同源检测的②级随入库预处理步另切片）。
  T4 多源重复：主谓宾全同 → 合并佐证（不建单）。
  T3 限定差异：双方 meta.scope 均有值且键不相交 → 两事实共存（不建单）；设计硬门禁「scope
  值须回指源文 span、LLM 推断 scope 不得自动生效」lite 放宽为键不相交（span 复核随 v1.5）。
  T2 真矛盾：其余 → 冲突工单（v1 全人工裁决；score 仅工单参考排序分，lite=confidence）。
- carryover（§8.1 事实级承接判定）：文档版本更新三分支，核心原则「文档被替代 ≠ 派生事实
  全部作废」，按 subject + predicate 对齐分组逐条判定、不整链推翻：
  a) 新版同主谓重现 → 旧事实 superseded + superseded_by 边（新版拆条时组级边记
     cardinality=split；kb_facts.status CheckConstraint 无 superseded 枚举，lite 状态以
     meta.carryover 承载，枚举扩展随 DDL 回填迁移）；
  b) 新版未重现 → 定向复查（lite 词法路：旧事实宾语值 contains 新版 chunks）：命中=
     suspected_miss（疑似漏抽加急），未命中=needs_review（默认可见待复核，不直接删除）；
  c) 新版明确否定 → T2 冲突工单（lite 判定式=否定语标记的确定性规则，语义判定随 v1.5）。
- apply_carryover：事务内落库（superseded_by 边 + meta.carryover 标记 + T2 工单行）；
  uk 幂等，调用方持有事务（仓库惯例 session.begin()），本函数只做行级写入、不自行提交。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.kb.data.governance_orm import KbConflict, KbFactRelation
from services.kb.data.orm import KbFact

# ---------------------------------------------------------------- 常量与结果类型

RELATION_SUPERSEDED_BY = "superseded_by"  # 版本承接（时间线/演变史通道只遍历此链，§8.2）
RELATION_OUTRANKED_BY = "outranked_by"  # 裁决淘汰（T2 出胜负时连边，lite 未启用）
RELATION_INVALIDATED_BY = "invalidated_by"  # 无替代撤回（工单废弃/scope 撤销，lite 未启用）

TRIAGE_T1 = "T1"
TRIAGE_T2 = "T2"
TRIAGE_T3 = "T3"
TRIAGE_T4 = "T4"
TRIAGE_NONE = "none"

RESOLUTION_PENDING = "pending"  # v1 工单只开单不裁决（§8.1 全人工）

# 承接判定标记（meta.carryover 值）：kb_facts.status CheckConstraint 无 needs_review/superseded
# 枚举，lite 一律以 meta 承载、不动硬约束（汇报已注明，枚举扩展随 DDL 回填迁移）
CARRYOVER_SUPERSEDED = "superseded"
CARRYOVER_NEEDS_REVIEW = "needs_review"
CARRYOVER_SUSPECTED_MISS = "suspected_miss"
CARRYOVER_CONFLICT = "conflict"

# c 分支「明确否定」lite 判定式：新版谓词/宾语含否定语标记即判明确矛盾（确定性规则；
# 误判代价可控——漏判走 T2 工单兜底而非误共存；v1.5 换语义判定，§8.1 承接判定 c 分支）
_NEGATION_MARKERS: tuple[str, ...] = ("不再", "废止", "取消", "不适用", "撤销", "作废")


@dataclass(frozen=True, slots=True)
class TriageResult:
    """单候选分诊结论（§8.1 四型四处置 + 无命中）。

    triage ∈ T1|T2|T3|T4|none；matched=命中的既有事实；relation=T1 时的边语义（superseded_by，
    方向=旧(被遮蔽方) superseded_by 新(候选)）；ticket=T2 时的冲突工单行值（fact_a=候选新方/
    fact_b=既有方，可直接 KbConflict(**ticket) 落库）；reason=判定依据（随工单/边 evidence
    落库可追溯，宪法 5）。
    """

    triage: str
    matched: dict[str, Any] | None = None
    relation: str | None = None
    ticket: dict[str, Any] | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CarryoverPlan:
    """承接判定计划（纯数据，落库经 apply_carryover；字段均为 ORM 行值/合并增量，可审计）。

    relations：KbFactRelation 行值（a 分支 superseded_by 边，evidence 含组级 cardinality）；
    fact_meta：(fact_id, meta 合并增量) 序对（a/b/c 分支的 meta.carryover 标记）；
    conflicts：KbConflict 行值（c 分支 T2 工单）。
    """

    relations: tuple[dict[str, Any], ...] = ()
    fact_meta: tuple[tuple[Any, dict[str, Any]], ...] = ()
    conflicts: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ApplyReport:
    """apply_carryover 落库回执（幂等重放断言面：重放时新建为 0、跳过为既有数）。"""

    relations: int = 0
    conflicts: int = 0
    facts_marked: int = 0
    skipped_relations: int = 0
    skipped_conflicts: int = 0


class GovernanceError(Exception):
    """知识治理落库错误（'NNN 消息' 口径对齐 pipeline_base.PipelineError）。"""


# ---------------------------------------------------------------- 冲突分诊（§8.1）


def detect_conflicts(cand: dict[str, Any], existing: list[dict[str, Any]]) -> TriageResult:
    """冲突分诊（§8.1 固定顺序 T1→T4→T3→T2；纯函数）。

    比对键 = subject + predicate；先取同键既有事实池，再按固定顺序判定，同型内取列表序首个
    命中（确定性，不依赖哈希序）。无同键命中 → triage=none（新知识，无冲突面）。
    """
    key = (cand.get("subject"), cand.get("predicate"))
    pool = [
        f for f in existing if f.get("id") != cand.get("id") and (f.get("subject"), f.get("predicate")) == key
    ]
    if not pool:
        return TriageResult(TRIAGE_NONE, reason=f"无比对键命中（subject={key[0]!r}, predicate={key[1]!r}）")
    for f in pool:  # T1 版本演进：lite=document_id 同源，自动建边新版遮蔽旧版，无需人工
        if _same_version_chain(cand, f):
            return TriageResult(
                TRIAGE_T1,
                matched=f,
                relation=RELATION_SUPERSEDED_BY,
                reason=(
                    "T1 同文档版本链（document_id="
                    f"{f.get('document_id')}）：建 superseded_by 边，新版遮蔽旧版，无需人工"
                ),
            )
    for f in pool:  # T4 多源重复：主谓宾全同 → 合并佐证（置信度上调，来源列表保留，不建单）
        if f.get("object") == cand.get("object"):
            return TriageResult(TRIAGE_T4, matched=f, reason="T4 主谓宾全同：多源重复，合并佐证（不建工单）")
    for f in pool:  # T3 限定差异：双方 scope 均有值且键不相交 → 共存（span 硬门禁复核随 v1.5）
        if _scope_disjoint(cand, f):
            return TriageResult(
                TRIAGE_T3,
                matched=f,
                reason=(
                    f"T3 scope 键不相交（{sorted(_scope_keys(cand))} × {sorted(_scope_keys(f))}）："
                    "限定差异，两事实共存各标 scope"
                ),
            )
    f = pool[0]  # T2 真矛盾：v1 全人工裁决，score 仅工单参考排序分（不触发自动动作，§8.1）
    return TriageResult(
        TRIAGE_T2,
        matched=f,
        ticket={
            "tenant_id": cand.get("tenant_id"),
            "conflict_type": TRIAGE_T2,
            "fact_a_id": cand.get("id"),
            "fact_b_id": f.get("id"),
            "score_a": _reference_score(cand),
            "score_b": _reference_score(f),
            "resolution": RESOLUTION_PENDING,
        },
        reason="T2 无版本关系语义互斥：冲突工单人工裁决（v1 不自动裁决）",
    )


def _same_version_chain(cand: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    """T1 判定 lite：document_id 同源（§8.0 版本链 lite 代理；identity_key 链接入后补跨版本）。"""
    doc = cand.get("document_id")
    return doc is not None and doc == other.get("document_id")


def _scope_keys(fact: Mapping[str, Any]) -> set[str]:
    """fact.meta.scope 封闭枚举键集（temporal/region/product_line/regulatory_domain[/source_system]）。"""
    meta = fact.get("meta")
    scope = meta.get("scope") if isinstance(meta, Mapping) else None
    return {str(k) for k in scope} if isinstance(scope, Mapping) and scope else set()


def _scope_disjoint(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """T3 判定：双方 scope 均有值且键不相交（键相交=同维度限定冲突 → 落 T2 人工）。"""
    keys_a, keys_b = _scope_keys(a), _scope_keys(b)
    return bool(keys_a) and bool(keys_b) and not keys_a & keys_b


def _reference_score(fact: Mapping[str, Any]) -> float:
    """T2 工单参考排序分 lite：confidence（门禁参数非真值，clamp 0~1）。

    §8.1 四项加权公式（0.35 来源权威 + 0.25 时效 + 0.20 佐证数 + 0.20 出处质量）待来源权威/
    佐证数信号入参后启用；v1 评分不触发任何自动动作（自动档=机器替人终审，v1.5 两前置条件）。
    """
    try:
        value = float(fact.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, value))


# ---------------------------------------------------------------- 承接判定（§8.1 事实级三分支）


def carryover(
    old_facts: list[dict[str, Any]],
    new_facts: list[dict[str, Any]],
    new_chunks: list[str] | None = None,
) -> CarryoverPlan:
    """文档版本更新承接判定（§8.1 三分支；纯函数）。

    old_facts=旧版文档派生的当前事实，new_facts=新版文档候选/终审事实；按 subject + predicate
    对齐分组逐条判定。b 分支定向复查用 new_chunks（新版 chunk 文本），不提供时按未命中保守
    处理（转 needs_review 复核，宁多复核不漏删）。
    """
    new_by_key: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for nf in new_facts:
        new_by_key.setdefault((nf.get("subject"), nf.get("predicate")), []).append(nf)

    relations: list[dict[str, Any]] = []
    fact_meta: list[tuple[Any, dict[str, Any]]] = []
    conflicts: list[dict[str, Any]] = []
    for old in old_facts:
        successors = new_by_key.get((old.get("subject"), old.get("predicate")), [])
        if successors:
            successor = successors[0]  # 组级边代表：新版拆条时首条承接，组员随边 evidence 可溯
            if _explicit_negation(old, successor):
                # c 分支：新版明确否定 → 冲突分诊（lite 固定落 T2 工单，§8.1）
                conflicts.append(
                    {
                        "tenant_id": old.get("tenant_id"),
                        "conflict_type": TRIAGE_T2,
                        "fact_a_id": successor.get("id"),
                        "fact_b_id": old.get("id"),
                        "score_a": _reference_score(successor),
                        "score_b": _reference_score(old),
                        "resolution": RESOLUTION_PENDING,
                    }
                )
                fact_meta.append(
                    (old.get("id"), {"carryover": CARRYOVER_CONFLICT, "conflict_with": str(successor.get("id"))})
                )
                continue
            relations.append(
                {
                    "tenant_id": old.get("tenant_id"),
                    "from_fact_id": old.get("id"),
                    "to_fact_id": successor.get("id"),
                    "relation": RELATION_SUPERSEDED_BY,
                    "evidence": {
                        "rule": "carryover_a",
                        # 新版把一条拆为多条：组级边记 cardinality=split（§8.1 承接判定 a）
                        "cardinality": "split" if len(successors) > 1 else "one_to_one",
                        "successor_group": [str(s.get("id")) for s in successors],  # JSONB 内 id 一律 str
                    },
                }
            )
            fact_meta.append(
                (
                    old.get("id"),
                    {"carryover": CARRYOVER_SUPERSEDED, "successor_fact_id": str(successor.get("id"))},
                )
            )
            continue
        # b 分支：新版未重现 → 定向复查（lite 词法路）→ 疑似漏抽 / 转待复核
        hit = _lexical_recheck(old, new_chunks or [])
        mark = CARRYOVER_SUSPECTED_MISS if hit else CARRYOVER_NEEDS_REVIEW
        fact_meta.append((old.get("id"), {"carryover": mark}))
    return CarryoverPlan(relations=tuple(relations), fact_meta=tuple(fact_meta), conflicts=tuple(conflicts))


def _explicit_negation(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    """c 分支「明确否定」lite 判定式：同主谓下新版谓词/宾语含否定语标记且宾语相异（确定性）。

    old 宾语为空（entity 型无值）不判否定，走 a 分支承接。
    """
    old_obj = old.get("object")
    new_obj = new.get("object")
    if not old_obj or not new_obj or new_obj == old_obj:
        return False
    haystack = f"{new.get('predicate') or ''} {new_obj}"
    return any(marker in haystack for marker in _NEGATION_MARKERS)


def _lexical_recheck(old: Mapping[str, Any], chunks: list[str]) -> bool:
    """b 分支定向复查 lite：旧事实宾语值（缺省回退术语/主语）在新版 chunks 内 contains 命中。

    命中=疑似漏抽（加急复核）；未命中=确认删除转 needs_review（默认可见不删除，§8.1）。
    """
    token = old.get("object") or old.get("canonical_name") or old.get("subject")
    if not token:
        return False
    return any(isinstance(chunk, str) and token in chunk for chunk in chunks)


# ---------------------------------------------------------------- 落库（事务内，uk 幂等）


async def apply_carryover(session: AsyncSession, plan: CarryoverPlan) -> ApplyReport:
    """承接计划事务内落库：superseded_by 边 + meta.carryover 标记 + T2 工单行（§8.1/§8.2）。

    幂等：边按 uk(from,to,relation)、工单按 uk(fact_a,fact_b) 先查后插（重放全跳过）；
    meta 为合并增量、重放同值。调用方持有事务（仓库惯例 `async with session.begin()`），
    本函数不自行 commit，仅 flush 令约束即时可见；事实行缺失抛 GovernanceError（响亮失败）。
    """
    relations = conflicts = facts_marked = skipped_relations = skipped_conflicts = 0

    existing_edges: set[tuple[Any, Any, str]] = set()
    if plan.relations:
        from_ids = {r["from_fact_id"] for r in plan.relations}
        rows = (
            await session.execute(
                select(KbFactRelation.from_fact_id, KbFactRelation.to_fact_id, KbFactRelation.relation).where(
                    KbFactRelation.from_fact_id.in_(from_ids)
                )
            )
        ).all()
        existing_edges = set(rows)
    for edge in plan.relations:
        key = (edge["from_fact_id"], edge["to_fact_id"], edge["relation"])
        if key in existing_edges:
            skipped_relations += 1
            continue
        session.add(KbFactRelation(**edge))
        relations += 1

    existing_conflicts: set[tuple[Any, Any]] = set()
    if plan.conflicts:
        fact_a_ids = {c["fact_a_id"] for c in plan.conflicts}
        rows = (
            await session.execute(
                select(KbConflict.fact_a_id, KbConflict.fact_b_id).where(KbConflict.fact_a_id.in_(fact_a_ids))
            )
        ).all()
        existing_conflicts = set(rows)
    for ticket in plan.conflicts:
        key = (ticket["fact_a_id"], ticket["fact_b_id"])
        if key in existing_conflicts:
            skipped_conflicts += 1
            continue
        session.add(KbConflict(**ticket))
        conflicts += 1

    for fact_id, patch in plan.fact_meta:
        fact = await session.get(KbFact, fact_id)
        if fact is None:
            raise GovernanceError(f"404 事实不存在: {fact_id}")
        fact.meta = {**(fact.meta or {}), **patch}  # JSONB 整体重赋值（kb_align 同款）
        facts_marked += 1

    await session.flush()
    return ApplyReport(
        relations=relations,
        conflicts=conflicts,
        facts_marked=facts_marked,
        skipped_relations=skipped_relations,
        skipped_conflicts=skipped_conflicts,
    )
