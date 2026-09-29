"""KB-G1a 冲突分诊四型落库版 + 事实级承接判定 + 冲突工单裁决执行（OntRAG §8.1 权威口径）。

与 business/governance.py（波次④ lite 纯函数）的关系：本模块是 §8.1 的会话内取数/落库实现——
分诊直接在 kb_facts 上比对（候选 vs 当前有效权威事实）、处置写行走 ORM 聚合先例（行 meta
整体重赋值 + kb_fact_relations 边，零裸 SQL 写）、T2 冲突工单走 review_tickets（target_type
='conflict'，枚举扩展随本批迁移）。lite 纯函数波次保留不动；两模块状态词汇值同源
（superseded_by/outranked_by 边值、封口由 meta+边 evidence 承载——kb_facts 双时间线列 DDL
待 DB owner 回填，OntRAG §6/§11）。

分诊固定顺序（§8.1 v0.2：先廉价确定性判定、后语义判定）：``T1 版本链命中 → T4 主谓宾全同
→ T3 双方 scope 均有源文落地且键不相交 → 否则 T2 工单``。比对键 = subject + predicate；
对象属性类（fact_type ∈ relation/event）宾语相异视为多值合法、不判真矛盾（§8.1 比对键注记）。

v1 简化点（实现口径，均登记设计对应节）：
- T1 版本链：① 同 document_id（同文档重抽）；② documents.meta["supersedes_id"] 版本链上溯
  （≤8 跳，database/01 supersedes_id 独立列回填后切列）；③ 结构化源行事件链 = evidence.
  source_ref 同 (source_system, external_id) 且 occurred_at 单调递增（多源接入 §5 同步，
  旧值 valid_to = 新值 occurred_at）。documents.valid_to 封口的继任关系由 ② 链覆盖表达。
- T3 硬门禁：scope 封闭枚举键（temporal/region/product_line/regulatory_domain/source_system）
  值须有源文 span 落地（scope 值形如 {"region": {"value": .., "span": [s,e)}} 或
  evidence["scope_spans"][key]）；source_system 键豁免 span——谓词须在 SYSTEM_RELATIVE_
  PREDICATES 白名单（v1 简化 = 配置常量，TBox systemRelative 标记接入后切换，§8.1 第 5 键）。
- T4 置信度上调 = min(1, max(双发置信度) + 0.05)（示例值/待实测）；候选行只标注 merged_into
  不翻状态——硬门禁「候选非成品」，机器不替人终审（宪法 3）。
- T2 评分四项 0.35/0.25/0.20/0.20（来源权威/时效/佐证数/出处质量）只作工单参考排序分写进
  payload，v1 全人工裁决、不触发任何自动动作（§8.1 v0.2 裁决）。
- 承接判定 b 分支词法复查 = 旧宾语+术语对新版 chunks 大小写不敏感 contains（ILIKE 等价口径，
  方言中立）；命中=疑似漏抽加急工单（target_type=knowledge_instance），未命中=转 needs_review
  （meta 承载，默认可见不删除）。c 分支「明确否定」按节文走分诊——同版本链对在分诊下定 T1
  自动承接，否定证据记入边 evidence。
- apply_decision：胜者裁决 = 败者封口 + outranked_by 边 + needs_review 标注（v1 简化为封口+
  标注亦可，本实现连边——§8.2 三边不复用）；工单状态推进归 review 决策端点（接线登记遗留）。

事务纪律：调用方持有事务（仓库惯例 ``async with session.begin()``），本模块只做行级写入、
末尾 flush 令约束即时可见、不自行 commit。工单端口 ``tickets`` 为鸭子类型满足
CandidateReviewPort.submit_candidate 面（review.data 模块私有，kb 零静态 import，契约六）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.kb.data.governance_orm import KbFactRelation
from services.kb.data.orm import Document, DocumentChunk, KbFact

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 常量（词汇值与 governance.py lite 同源）

TRIAGE_T1 = "T1"  # 版本演进（过时）：自动建 SUPERSEDES 承接，无工单
TRIAGE_T2 = "T2"  # 真矛盾：v1 全人工裁决工单
TRIAGE_T3 = "T3"  # 限定差异：双保留各标 scope，无工单
TRIAGE_T4 = "T4"  # 多源重复：合并佐证，无工单
TRIAGE_NONE = "none"  # 无冲突面（无比对键命中 / 对象属性多值合法）

RELATION_SUPERSEDED_BY = "superseded_by"  # 版本承接（时间线通道只遍历此链，§8.2）
RELATION_OUTRANKED_BY = "outranked_by"  # 人工裁决淘汰（不混入版本演变史，§8.2）

# 事实行 meta.conflict_triage.state 词汇（kb_facts.status CheckConstraint 无对应枚举，
# 封口/复核状态由 meta 承载——同 governance.py lite 先例，DDL 回填随 DB owner）
STATE_SUPERSEDED = "superseded"  # T1/a 分支封口（遮蔽而非消失，§8.2）
STATE_OUTRANKED = "outranked"  # T2 人工裁决败者封口（附 needs_review 标注）
STATE_COEXIST = "t3_coexist"  # T3 限定共存
STATE_NEEDS_REVIEW = "needs_review"  # b 分支未命中：默认可见待复核，不删除
STATE_MERGED_INTO = "merged_into"  # T4 候选方：佐证已并入既有权威事实
STATE_CORROBORATED = "corroborated"  # T4 既有方：多源佐证合并
_CLOSED_STATES = frozenset({STATE_SUPERSEDED, STATE_OUTRANKED})  # 不再入分诊池/承接池

# scope 封闭枚举键（§8.1：键值 map；source_system 为多源接入第 5 键，豁免 span 落地）
SCOPE_KEYS = frozenset({"temporal", "region", "product_line", "regulatory_domain", "source_system"})
# v1 简化白名单：TBox systemRelative 标记接入前列（§8.1 source_system 豁免的谓词级声明制）
SYSTEM_RELATIVE_PREDICATES = frozenset({"localStatus", "localCategory"})

# T2 参考评分四项权重（§8.1；初始建议值/待实测，PoC ② 校准冻结前不得触发自动动作）
SCORE_WEIGHTS: dict[str, float] = {"authority": 0.35, "recency": 0.25, "corroboration": 0.20, "citation": 0.20}

T4_CONFIDENCE_BOOST = 0.05  # T4 合并佐证置信度上调（示例值/待实测）
TARGET_TYPE_CONFLICT = "conflict"  # review_tickets target_type 枚举扩展（本批迁移）
TARGET_TYPE_KNOWLEDGE = "knowledge_instance"  # b 分支疑似漏抽加急工单复用既有枚举

CARRYOVER_BRANCH_A = "a"  # 同主谓新事实：旧封口 + superseded_by
CARRYOVER_BRANCH_B_HIT = "b_hit"  # 未重现+词法命中：疑似漏抽加急工单
CARRYOVER_BRANCH_B_MISS = "b_miss"  # 未重现+未命中：转 needs_review
CARRYOVER_BRANCH_C = "c"  # 明确否定：走冲突分诊（同链对 → T1 承接 + 否定证据）

DECISION_WINNER_A = "winner_a"
DECISION_WINNER_B = "winner_b"
DECISION_T3_COEXIST = "t3_coexist"
DECISION_PENDING = "pending"
DECISION_OPTIONS = (DECISION_WINNER_A, DECISION_WINNER_B, DECISION_T3_COEXIST, DECISION_PENDING)

# c 分支「明确否定」v1 判定式（确定性规则，§8.1 承接判定 c；v1.5 换语义判定）
_NEGATION_MARKERS: tuple[str, ...] = ("不再", "废止", "取消", "不适用", "撤销", "作废")
_OBJECT_FACT_TYPES = frozenset({"relation", "event"})  # 对象属性类：宾语相异=多值合法非矛盾
_VERSION_CHAIN_MAX_HOPS = 8

__all__ = [
    "CARRYOVER_BRANCH_A",
    "CARRYOVER_BRANCH_B_HIT",
    "CARRYOVER_BRANCH_B_MISS",
    "CARRYOVER_BRANCH_C",
    "CarryoverOutcome",
    "CarryoverReport",
    "ConflictTriageError",
    "DECISION_OPTIONS",
    "DECISION_PENDING",
    "DECISION_T3_COEXIST",
    "DECISION_WINNER_A",
    "DECISION_WINNER_B",
    "DecisionReport",
    "RELATION_OUTRANKED_BY",
    "RELATION_SUPERSEDED_BY",
    "SCOPE_KEYS",
    "SCORE_WEIGHTS",
    "STATE_COEXIST",
    "STATE_CORROBORATED",
    "STATE_MERGED_INTO",
    "STATE_NEEDS_REVIEW",
    "STATE_OUTRANKED",
    "STATE_SUPERSEDED",
    "SYSTEM_RELATIVE_PREDICATES",
    "TARGET_TYPE_CONFLICT",
    "TRIAGE_NONE",
    "TRIAGE_T1",
    "TRIAGE_T2",
    "TRIAGE_T3",
    "TRIAGE_T4",
    "apply_decision",
    "carryover_facts",
    "triage_conflicts",
]


class ConflictTriageError(Exception):
    """冲突分诊/承接/裁决数据错误（'NNN 消息' 口径对齐 pipeline_base.PipelineError）。"""


# ---------------------------------------------------------------- 结果载荷（business 层只数据无行为）


@dataclass(frozen=True, slots=True)
class TriageOutcome:
    """单候选分诊结论：triage ∈ T1|T2|T3|T4|none + 命中既有事实 + 工单 id + 判定依据。"""

    candidate_fact_id: uuid.UUID
    triage: str
    matched_fact_id: uuid.UUID | None = None
    ticket_id: uuid.UUID | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TriageReport:
    """一次分诊回执（outcomes 逐候选可审计；重放安全：已分诊候选跳过）。"""

    outcomes: tuple[TriageOutcome, ...] = ()

    @property
    def tickets_opened(self) -> int:
        return sum(1 for o in self.outcomes if o.ticket_id is not None)


@dataclass(frozen=True, slots=True)
class CarryoverOutcome:
    """单旧事实承接结论：branch ∈ a|b_hit|b_miss|c（§8.1 事实级承接判定三分支）。"""

    fact_id: uuid.UUID
    branch: str
    successor_fact_id: uuid.UUID | None = None
    ticket_id: uuid.UUID | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CarryoverReport:
    """承接判定回执（无版本链/无旧事实 = 空 outcomes 合法态）。"""

    outcomes: tuple[CarryoverOutcome, ...] = ()


@dataclass(frozen=True, slots=True)
class DecisionReport:
    """冲突工单裁决执行回执（payload_patch 供调用方合并回工单信封做裁决留痕）。"""

    ticket_id: uuid.UUID
    resolution: str
    winner_fact_id: uuid.UUID | None = None
    loser_fact_id: uuid.UUID | None = None
    facts_annotated: int = 0
    edges_created: int = 0
    payload_patch: dict[str, Any] | None = None


# ---------------------------------------------------------------- 视图与纯判定辅助（dict 视图统一 ORM/入参两形）


def _view(row: KbFact | Mapping[str, Any]) -> dict[str, Any]:
    """KbFact ORM 行或行值 dict → 统一视图 dict（判定辅助只消费这些键）。"""
    if isinstance(row, Mapping):
        return dict(row)
    return {
        "id": row.id,
        "document_id": row.document_id,
        "fact_type": row.fact_type,
        "subject": row.subject,
        "predicate": row.predicate,
        "object": row.object,
        "aliases": list(row.aliases or []),
        "confidence": float(row.confidence or 0),
        "evidence": dict(row.evidence or {}),
        "meta": dict(row.meta or {}),
        "created_at": row.created_at,
    }


def _state_of(fact: Mapping[str, Any]) -> str:
    """事实行 meta.conflict_triage.state（无标注 = 空串，未分诊/未承接）。"""
    ct = (fact.get("meta") or {}).get("conflict_triage")
    return str(ct.get("state") or "") if isinstance(ct, Mapping) else ""


def _source_ref(fact: Mapping[str, Any]) -> Mapping[str, Any]:
    sr = (fact.get("evidence") or {}).get("source_ref")
    return sr if isinstance(sr, Mapping) else {}


def _scope_of(fact: Mapping[str, Any]) -> dict[str, Any]:
    scope = (fact.get("meta") or {}).get("scope")
    return dict(scope) if isinstance(scope, Mapping) and scope else {}


def _parse_ts(value: Any) -> datetime | None:
    """ISO 字符串/datetime → datetime（不可解析返回 None，保守不判递增）。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _ts_ge(a: Any, b: Any) -> bool:
    """a ≥ b（时间戳单调递增判定；datetime 优先，双 str 回退字典序，不可比 = False）。"""
    ta, tb = _parse_ts(a), _parse_ts(b)
    if ta is not None and tb is not None:
        return ta >= tb
    if isinstance(a, str) and isinstance(b, str):
        return a >= b  # ISO 同格式字典序 ≡ 时序
    return False


def _structured_chain(cand: Mapping[str, Any], old: Mapping[str, Any]) -> bool:
    """结构化源 T1 判定（多源接入 §5）：同 (source_system, external_id) 且 occurred_at 单调递增。"""
    sr_c, sr_o = _source_ref(cand), _source_ref(old)
    if not (sr_c.get("source_system") and sr_c.get("external_id")):
        return False
    if (sr_c.get("source_system"), sr_c.get("external_id")) != (sr_o.get("source_system"), sr_o.get("external_id")):
        return False
    occ_c, occ_o = sr_c.get("occurred_at"), sr_o.get("occurred_at")
    return occ_c is not None and occ_o is not None and _ts_ge(occ_c, occ_o)


def _scope_value(value: Any) -> Any:
    """scope 值解包：{"value":..,"span":..} 形取 value，平值原样返回。"""
    return value.get("value") if isinstance(value, Mapping) else value


def _scope_grounded(fact: Mapping[str, Any]) -> tuple[bool, str]:
    """T3 硬门禁：scope 值有源文 span 落地，或 source_system 豁免（谓词白名单，§8.1）。

    返回 (是否落地, 判定依据)；LLM 推断的 scope 无 span 不得自动生效（防 T2 被误判 T3）。
    source_system 键豁免 span：取值来自连接器信封确定性元数据（非 LLM 推断），但须谓词在
    白名单（v1 简化 = SYSTEM_RELATIVE_PREDICATES 常量，TBox systemRelative 标记接入后切换）。
    """
    scope = _scope_of(fact)
    if not scope:
        return False, "无 scope"
    system_relative = fact.get("predicate") in SYSTEM_RELATIVE_PREDICATES
    spans = (fact.get("evidence") or {}).get("scope_spans")
    spans = spans if isinstance(spans, Mapping) else {}
    for key, value in scope.items():
        if key == "source_system" and system_relative:
            continue  # 第 5 键豁免（§8.1 多源接入同步）
        span = value.get("span") if isinstance(value, Mapping) else None
        if not span and not spans.get(key):
            return False, f"scope 键 {key} 无源文 span 落地"
    return True, "span 落地/source_system 豁免"


def _t3_coexist(cand: Mapping[str, Any], old: Mapping[str, Any]) -> tuple[bool, str]:
    """T3 判定（§8.1）：① 跨系统豁免通道——双方均带 source_system 且值不同 + 谓词白名单
    （跨系统同主谓异值自动共存，键同值异不适用键不相交规则）；② 常规通道——scope 键
    不相交且各自值均有源文 span 落地。"""
    scope_c, scope_o = _scope_of(cand), _scope_of(old)
    if "source_system" in scope_c and "source_system" in scope_o:
        system_pair = (_scope_value(scope_c["source_system"]), _scope_value(scope_o["source_system"]))
        if cand.get("predicate") in SYSTEM_RELATIVE_PREDICATES and system_pair[0] != system_pair[1]:
            return True, "T3 source_system 豁免（跨系统同主谓异值，谓词白名单）"
        return False, "双方同带 source_system 但谓词不在白名单或值相同"
    keys_c, keys_o = set(scope_c), set(scope_o)
    if not keys_c or not keys_o or keys_c & keys_o:
        return False, "scope 键缺失或相交"
    grounded_c, why_c = _scope_grounded(cand)
    grounded_o, why_o = _scope_grounded(old)
    if grounded_c and grounded_o:
        return True, f"T3 scope 键不相交且落地（{sorted(keys_c)} × {sorted(keys_o)}）"
    return False, f"scope 未落地（候选：{why_c}；既有：{why_o}）"


def _explicit_negation(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    """c 分支「明确否定」v1 判定式：同主谓下新谓词/宾语含否定标记且宾语相异（确定性规则）。"""
    old_obj, new_obj = old.get("object"), new.get("object")
    if not old_obj or not new_obj or new_obj == old_obj:
        return False
    haystack = f"{new.get('predicate') or ''} {new_obj}"
    return any(marker in haystack for marker in _NEGATION_MARKERS)


# ---------------------------------------------------------------- T2 参考评分（§8.1 四项加权；仅排序参考，不触发动作）


def _authority_score(fact: Mapping[str, Any]) -> float:
    """来源权威 v1 简化：meta.authority_score 覆盖 > 文档 source_type（api=结构化源 0.8/upload 0.5）。"""
    override = (fact.get("meta") or {}).get("authority_score")
    if isinstance(override, (int, float)):
        return min(1.0, max(0.0, float(override)))
    return 0.8 if (fact.get("source_type") or _source_ref(fact).get("source_type")) == "api" else 0.5


def _recency_score(fact: Mapping[str, Any], peer: Mapping[str, Any]) -> float:
    """时效 v1 简化：occurred_at/created_at 新近者 1.0、较旧者 0.0（同刻 0.5，二元示例值）。"""
    t_self = _parse_ts(_source_ref(fact).get("occurred_at")) or fact.get("created_at")
    t_peer = _parse_ts(_source_ref(peer).get("occurred_at")) or peer.get("created_at")
    if t_self is None or t_peer is None or t_self == t_peer:
        return 0.5
    return 1.0 if t_self > t_peer else 0.0


def _corroboration_score(fact: Mapping[str, Any]) -> float:
    """佐证数 v1 简化：独立 source_ref 数（1 + T4 合并数），3 条佐证满分（示例值）。"""
    merged = (fact.get("evidence") or {}).get("merged_source_refs")
    count = 1 + (len(merged) if isinstance(merged, list) else 0)
    return min(1.0, count / 3)


def _citation_score(fact: Mapping[str, Any]) -> float:
    """出处质量 v1 简化：span 指向条款原文 1.0 > 仅引语 0.5 > 转述无引 0.0（§8.1 出处质量项）。"""
    evidence = fact.get("evidence") or {}
    if evidence.get("span"):
        return 1.0
    return 0.5 if evidence.get("quote") else 0.0


def _reference_scores(fact: Mapping[str, Any], peer: Mapping[str, Any]) -> dict[str, Any]:
    """单侧四项加权参考分（总分 Σ w·s；全部为示例值/待实测，仅作工单 UI 排序参考）。"""
    parts = {
        "authority": _authority_score(fact),
        "recency": _recency_score(fact, peer),
        "corroboration": _corroboration_score(fact),
        "citation": _citation_score(fact),
    }
    total = sum(SCORE_WEIGHTS[k] * v for k, v in parts.items())
    return {**parts, "total": round(total, 4), "weights": dict(SCORE_WEIGHTS)}


def _fact_digest(fact: Mapping[str, Any]) -> dict[str, Any]:
    """工单并排呈现的事实摘要（主谓宾 + 各自出处 + scope，§8.1「两条事实+各自原文出处」）。"""
    evidence = fact.get("evidence") or {}
    return {
        "id": str(fact.get("id")),
        "fact_type": fact.get("fact_type"),
        "subject": fact.get("subject"),
        "predicate": fact.get("predicate"),
        "object": fact.get("object"),
        "scope": _scope_of(fact),
        "source_ref": dict(_source_ref(fact)),
        "quote": evidence.get("quote"),
        "span": evidence.get("span"),
        "confidence": fact.get("confidence"),
        "created_at": fact.get("created_at"),
    }


# ---------------------------------------------------------------- 会话上下文（文档版本链缓存 + 工单端口）


class _Ctx:
    """单次分诊/承接的会话上下文：文档行缓存 + 工单端口 + trace_id（避免重复取数）。"""

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID, tickets: Any, trace_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.tickets = tickets
        self.trace_id = trace_id
        self._docs: dict[uuid.UUID, Document | None] = {}

    async def doc(self, doc_id: uuid.UUID | None) -> Document | None:
        if doc_id is None:
            return None
        if doc_id not in self._docs:
            self._docs[doc_id] = await self.session.get(Document, doc_id)
        return self._docs[doc_id]

    async def version_chain(self, cand_doc_id: Any, old_doc_id: Any) -> tuple[bool, str]:
        """T1 版本链判定：同文档 / meta.supersedes_id 链上溯 / 结构化源行事件链。"""
        if cand_doc_id is not None and cand_doc_id == old_doc_id:
            return True, "same_document"
        cur = await self.doc(cand_doc_id)  # type: ignore[arg-type]
        for _ in range(_VERSION_CHAIN_MAX_HOPS):
            if cur is None:
                break
            parent_raw = (cur.meta or {}).get("supersedes_id")
            if not parent_raw:
                break
            try:
                parent_id = uuid.UUID(str(parent_raw))
            except ValueError:
                break  # 链畸形：保守终止上溯（不判 T1）
            if parent_id == old_doc_id:
                return True, "document_chain"
            cur = await self.doc(parent_id)
        return False, ""


# ---------------------------------------------------------------- 写路径辅助（ORM 行写入，零裸 SQL）


def _merge_meta(fact: KbFact, patch: dict[str, Any]) -> None:
    """meta 合并增量整体重赋值（JSONB 不可原地变更，kb_align/apply_carryover 同款）。"""
    meta = dict(fact.meta or {})
    ct = dict(meta.get("conflict_triage") or {})
    meta["conflict_triage"] = {**ct, **patch}
    fact.meta = meta


async def _add_edge(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    relation: str,
    evidence: dict[str, Any],
) -> bool:
    """失效分边幂等落库（uk(from,to,relation) 先查后插；重放跳过返回 False）。"""
    exists = (
        await session.execute(
            select(KbFactRelation.id).where(
                KbFactRelation.from_fact_id == from_id,
                KbFactRelation.to_fact_id == to_id,
                KbFactRelation.relation == relation,
            )
        )
    ).scalar_one_or_none()
    if exists is not None:
        return False
    session.add(
        KbFactRelation(
            tenant_id=tenant_id, from_fact_id=from_id, to_fact_id=to_id, relation=relation, evidence=evidence
        )
    )
    return True


def _successor_effective_time(cand: Mapping[str, Any], cand_doc: Document | None) -> tuple[str, bool]:
    """继任业务生效时间（封口 valid_to，§8.2 封口规则）：结构源 occurred_at > 候选/文档
    valid_from(effective_date) > 处置时刻回退（记 effective_date_known=False 供审计区分）。"""
    occ = _source_ref(cand).get("occurred_at")
    ts = _parse_ts(occ)
    if ts is not None:
        return ts.isoformat(), True
    for candidate_value in ((cand.get("meta") or {}).get("valid_from"), (cand.get("meta") or {}).get("effective_date")):
        ts = _parse_ts(candidate_value)
        if ts is not None:
            return ts.isoformat(), True
    if cand_doc is not None:
        ts = _parse_ts(cand_doc.valid_from) or _parse_ts((cand_doc.meta or {}).get("effective_date"))
        if ts is not None:
            return ts.isoformat(), True
    return datetime.now(UTC).isoformat(), False


# ---------------------------------------------------------------- 冲突分诊主入口（§8.1 四型四处置）


async def triage_conflicts(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    fact_pairs: list[Mapping[str, Any]] | None = None,
    doc_id: uuid.UUID | None = None,
    tickets: Any = None,
    trace_id: str = "",
    candidate_statuses: tuple[str, ...] = ("candidate",),
) -> TriageReport:
    """候选事实 vs 当前有效权威事实分诊（§8.1 固定顺序 T1→T4→T3→T2；调用方持有事务）。

    - ``fact_pairs``：显式候选事实行值列表（须含 id——T1/T4 落边与 T2 建单均需已持久化 id）；
    - ``doc_id``：取该文档候选行（status ∈ candidate_statuses，步骤 5 终审前比对口径）；
    - ``tickets``：CandidateReviewPort.submit_candidate 鸭子类型端口（T2 建单必需，缺失时
      T2 响亮失败不静默——kb_extraction run_extract 同款纪律）；
    - 重放安全：已带 conflict_triage 标注的候选跳过；边 uk / 工单 uk_review_one_open 幂等。
    """
    if (fact_pairs is None) == (doc_id is None):
        raise ValueError("3001 PARAM_INVALID: fact_pairs 与 doc_id 必须二选一传入")
    if fact_pairs is not None:
        cand_rows: list[KbFact] = []
        for item in fact_pairs:
            fid = item.get("id")
            if fid is None:
                raise ValueError("3001 PARAM_INVALID: fact_pairs 项须含已持久化 id（落边/建单依赖）")
            row = await session.get(KbFact, uuid.UUID(str(fid)))
            if row is None or row.tenant_id != tenant_id:
                raise ConflictTriageError(f"404 候选事实不存在: {fid}")
            cand_rows.append(row)
    else:
        cand_rows = (
            (
                await session.execute(
                    select(KbFact)
                    .where(
                        KbFact.tenant_id == tenant_id,
                        KbFact.document_id == doc_id,
                        KbFact.status.in_(candidate_statuses),
                    )
                    .order_by(KbFact.created_at, KbFact.id)
                )
            )
            .scalars()
            .all()
        )

    ctx = _Ctx(session, tenant_id, tickets, trace_id)
    outcomes: list[TriageOutcome] = []
    for row in cand_rows:
        if _state_of(_view(row)):  # 已分诊候选（merged_into/t3_coexist 等）跳过：重放安全
            continue
        outcomes.append(await _triage_one(ctx, row))

    await session.flush()
    return TriageReport(outcomes=tuple(outcomes))


async def _load_pool(session: AsyncSession, tenant_id: uuid.UUID, cand: Mapping[str, Any]) -> list[dict[str, Any]]:
    """同 (subject, predicate) 当前有效权威事实池（status=authoritative、未封口；不含候选自身）。"""
    predicate_cond = (
        KbFact.predicate.is_(None) if cand.get("predicate") is None else KbFact.predicate == cand.get("predicate")
    )
    rows = (
        (
            await session.execute(
                select(KbFact)
                .where(
                    KbFact.tenant_id == tenant_id,
                    KbFact.subject == cand.get("subject"),
                    predicate_cond,
                    KbFact.status == "authoritative",
                    KbFact.id != cand.get("id"),
                )
                .order_by(KbFact.created_at, KbFact.id)
            )
        )
        .scalars()
        .all()
    )
    return [v for v in (_view(r) for r in rows) if _state_of(v) not in _CLOSED_STATES]


async def _triage_one(ctx: _Ctx, cand_row: KbFact, *, note: str | None = None) -> TriageOutcome:
    """单候选按固定顺序分诊并落处置（T1 承接/T4 合并/T3 共存/T2 工单/none）。

    ``note`` = 承接判定 c 分支等调用方的补充依据（并入边 evidence 与封口 meta，可追溯）。
    """
    cand = _view(cand_row)
    pool = await _load_pool(ctx.session, ctx.tenant_id, cand)
    if not pool:
        return TriageOutcome(cand["id"], TRIAGE_NONE, reason=f"无比对键命中（subject={cand['subject']!r}）")

    for old in pool:  # T1 版本演进：自动建事实级 SUPERSEDES 承接，无工单（§8.1）
        chained, chain_kind = await ctx.version_chain(cand.get("document_id"), old.get("document_id"))
        if not chained:
            chained, chain_kind = _structured_chain(cand, old), "structured_source"
        if chained:
            valid_to, known = _successor_effective_time(cand, await ctx.doc(cand.get("document_id")))
            old_row = await ctx.session.get(KbFact, old["id"])
            assert old_row is not None  # 池行来自本会话查询
            _merge_meta(
                old_row,
                {
                    "state": STATE_SUPERSEDED,
                    "valid_to": valid_to,
                    "effective_date_known": known,
                    "successor_fact_id": str(cand["id"]),
                    "rule": f"t1_{chain_kind}",
                    **({"note": note} if note else {}),
                },
            )
            await _add_edge(
                ctx.session,
                ctx.tenant_id,
                old["id"],
                cand["id"],
                RELATION_SUPERSEDED_BY,
                {
                    "rule": "conflict_triage_t1",
                    "chain": chain_kind,
                    "valid_to": valid_to,
                    "effective_date_known": known,
                    **({"note": note} if note else {}),
                },
            )
            return TriageOutcome(
                cand["id"],
                TRIAGE_T1,
                matched_fact_id=old["id"],
                reason=f"T1 版本链命中（{chain_kind}）：旧事实封口 valid_to={valid_to} + superseded_by 边，无工单",
            )

    for old in pool:  # T4 多源重复：主谓宾全同 → 合并佐证（多 source_ref + 置信度上调），无工单
        if old.get("object") == cand.get("object"):
            old_row = await ctx.session.get(KbFact, old["id"])
            assert old_row is not None
            evidence = dict(old_row.evidence or {})
            merged = list(evidence.get("merged_source_refs") or [])
            merged.append(dict(_source_ref(cand)))
            evidence["merged_source_refs"] = merged
            old_row.evidence = evidence  # JSONB 整体重赋值
            old_row.confidence = min(1.0, max(float(old_row.confidence or 0), cand["confidence"]) + T4_CONFIDENCE_BOOST)
            _merge_meta(old_row, {"state": STATE_CORROBORATED, "corroboration_count": len(merged)})
            # 候选不翻状态：硬门禁人工终审（机器不替人终审，宪法 3）
            _merge_meta(cand_row, {"state": STATE_MERGED_INTO, "target_fact_id": str(old["id"])})
            return TriageOutcome(
                cand["id"],
                TRIAGE_T4,
                matched_fact_id=old["id"],
                reason=(
                    f"T4 主谓宾全同：佐证合并（source_ref ×{len(merged)}，"
                    f"置信度上调 +{T4_CONFIDENCE_BOOST}），无工单"
                ),
            )

    for old in pool:  # T3 限定差异：scope 键不相交且值有源文落地（硬门禁）→ 双保留各标 scope，无工单
        ok, why = _t3_coexist(cand, old)
        if ok:
            old_row = await ctx.session.get(KbFact, old["id"])
            assert old_row is not None
            _merge_meta(old_row, {"state": STATE_COEXIST, "with_fact_id": str(cand["id"])})
            _merge_meta(cand_row, {"state": STATE_COEXIST, "with_fact_id": str(old["id"])})
            return TriageOutcome(
                cand["id"], TRIAGE_T3, matched_fact_id=old["id"], reason=f"{why}：双保留各标 scope，无工单"
            )

    old = pool[0]  # T2 真矛盾：v1 全部建冲突工单人工裁决（评分仅参考，§8.1 v0.2）
    if cand.get("fact_type") in _OBJECT_FACT_TYPES and old.get("object") != cand.get("object"):
        return TriageOutcome(
            cand["id"],
            TRIAGE_NONE,
            matched_fact_id=old["id"],
            reason="对象属性类主谓同宾语异：多值合法，非真矛盾（§8.1 比对键注记）",
        )
    if ctx.tickets is None:
        raise ConflictTriageError("409 冲突工单端口未装配（T2 建单必需，kb_extraction run_extract 同款纪律）")
    payload = {
        "envelope_version": "v1",
        "candidate_type": TARGET_TYPE_CONFLICT,
        "template_ref": "conflict_triage@v1",
        "trace_id": ctx.trace_id,
        "payload": {
            "conflict_type": TRIAGE_T2,
            "fact_a_id": str(cand["id"]),
            "fact_b_id": str(old["id"]),
            "fact_a": _fact_digest(cand),
            "fact_b": _fact_digest(old),
            "score_detail": {
                "fact_a": _reference_scores(cand, old),
                "fact_b": _reference_scores(old, cand),
                "note": "参考排序分（示例值/待实测），v1 全人工裁决、不触发自动动作（§8.1）",
            },
        },
        "decision_options": list(DECISION_OPTIONS),
        "review": {"state": "pending_review"},
    }
    ticket_id = await ctx.tickets.submit_candidate(
        tenant_id=ctx.tenant_id, target_type=TARGET_TYPE_CONFLICT, target_id=cand["id"], payload=payload
    )
    return TriageOutcome(
        cand["id"],
        TRIAGE_T2,
        matched_fact_id=old["id"],
        ticket_id=ticket_id,
        reason="T2 无版本关系语义互斥：冲突工单人工裁决",
    )


# ---------------------------------------------------------------- 事实级承接判定（§8.1 a/b/c 三分支）


async def carryover_facts(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    doc_id: uuid.UUID,
    *,
    new_chunks: list[str] | None = None,
    tickets: Any = None,
    trace_id: str = "",
    new_fact_statuses: tuple[str, ...] = ("authoritative",),
) -> CarryoverReport:
    """新版文档终审后的事实级承接判定（§8.1：文档被替代 ≠ 派生事实全部作废，逐条判定）。

    版本链入口 = 新版 documents.meta["supersedes_id"]（database/01 supersedes_id 列回填后切列）；
    旧事实池 = 旧版派生的当前有效权威事实（status=authoritative 且未封口）；新事实池 =
    新版 status ∈ new_fact_statuses（终审后缺省 authoritative）。b 分支词法复查用
    ``new_chunks``（缺省查 document_chunks，读直查）；工单端口 ``tickets`` 同分诊（b 命中必需）。
    """
    new_doc = await session.get(Document, doc_id)
    if new_doc is None or new_doc.tenant_id != tenant_id:
        raise ConflictTriageError(f"404 文档不存在: {doc_id}")
    old_id_raw = (new_doc.meta or {}).get("supersedes_id")
    try:
        old_doc_id = uuid.UUID(str(old_id_raw)) if old_id_raw else None
    except ValueError:
        old_doc_id = None
    if old_doc_id is None:
        return CarryoverReport()  # 无版本链：无事可做（合法态，幂等）

    old_rows = (
        (
            await session.execute(
                select(KbFact)
                .where(
                    KbFact.tenant_id == tenant_id,
                    KbFact.document_id == old_doc_id,
                    KbFact.status == "authoritative",
                )
                .order_by(KbFact.created_at, KbFact.id)
            )
        )
        .scalars()
        .all()
    )
    new_rows = (
        (
            await session.execute(
                select(KbFact)
                .where(
                    KbFact.tenant_id == tenant_id,
                    KbFact.document_id == doc_id,
                    KbFact.status.in_(new_fact_statuses),
                )
                .order_by(KbFact.created_at, KbFact.id)
            )
        )
        .scalars()
        .all()
    )
    new_by_key: dict[tuple[Any, Any], list[KbFact]] = {}
    for row in new_rows:
        new_by_key.setdefault((row.subject, row.predicate), []).append(row)

    ctx = _Ctx(session, tenant_id, tickets, trace_id)
    outcomes: list[CarryoverOutcome] = []
    for old_row in old_rows:
        old = _view(old_row)
        if _state_of(old) in _CLOSED_STATES:
            continue  # 已封口（前次承接/T2 裁决）：重放跳过
        successors = new_by_key.get((old_row.subject, old_row.predicate), [])
        if successors:
            succ_row = successors[0]
            succ = _view(succ_row)
            if _explicit_negation(old, succ):
                # c 分支：新版明确否定 → 走冲突分诊（§8.1；同版本链对分诊定 T1 自动承接，否定证据入边）
                outcome = await _triage_one(ctx, succ_row, note="carryover_c_explicit_negation")
                outcomes.append(
                    CarryoverOutcome(
                        old["id"],
                        CARRYOVER_BRANCH_C,
                        successor_fact_id=succ["id"],
                        ticket_id=outcome.ticket_id,
                        reason=f"c 明确否定走分诊 → {outcome.triage}（{outcome.reason}）",
                    )
                )
                continue
            # a 分支：新版同主谓重现 → 旧封口（valid_to=继任业务生效时间）+ superseded_by（拆条记 split）
            valid_to, known = _successor_effective_time(succ, new_doc)
            _merge_meta(
                old_row,
                {
                    "state": STATE_SUPERSEDED,
                    "valid_to": valid_to,
                    "effective_date_known": known,
                    "successor_fact_id": str(succ["id"]),
                    "rule": "carryover_a",
                },
            )
            await _add_edge(
                session,
                tenant_id,
                old["id"],
                succ["id"],
                RELATION_SUPERSEDED_BY,
                {
                    "rule": "carryover_a",
                    "cardinality": "split" if len(successors) > 1 else "one_to_one",
                    "successor_group": [str(s.get("id")) for s in (_view(r) for r in successors)],
                    "valid_to": valid_to,
                    "effective_date_known": known,
                },
            )
            outcomes.append(
                CarryoverOutcome(
                    old["id"],
                    CARRYOVER_BRANCH_A,
                    successor_fact_id=succ["id"],
                    reason=f"a 同主谓重现：旧封口 valid_to={valid_to} + superseded_by（×{len(successors)}）",
                )
            )
            continue
        # b 分支：新版未重现 → 词法复查（旧宾语+术语对新版 chunks 大小写不敏感 contains，ILIKE 等价）
        # chunks 惰性解析：调用方未显式传入时首遇 b 分支再直查 document_chunks（读直查；全承接/全否定零查询）
        tokens = [t for t in [old.get("object"), *(old.get("aliases") or [])] if t]
        if new_chunks is None:
            chunk_rows = (
                await session.execute(select(DocumentChunk.content).where(DocumentChunk.document_id == doc_id))
            ).all()
            new_chunks = [row[0] for row in chunk_rows]
        lowered = [c.lower() for c in new_chunks if isinstance(c, str)]
        hit = any(tok.lower() in chunk for tok in tokens for chunk in lowered) if tokens else False
        if hit:
            if tickets is None:
                raise ConflictTriageError("409 加急工单端口未装配（b 命中建单必需）")
            payload = {
                "envelope_version": "v1",
                "candidate_type": TARGET_TYPE_KNOWLEDGE,
                "template_ref": "conflict_triage@v1",
                "trace_id": trace_id,
                "payload": {
                    "suspected_miss": True,
                    "urgent": True,
                    "fact": _fact_digest(old),
                    "old_doc_id": str(old_doc_id),
                    "new_doc_id": str(doc_id),
                    "reason": "承接判定 b 分支：旧事实宾语/术语在新版 chunks 词法命中，疑似新版漏抽（§8.1）",
                },
                "review": {"state": "pending_review"},
            }
            ticket_id = await tickets.submit_candidate(
                tenant_id=tenant_id, target_type=TARGET_TYPE_KNOWLEDGE, target_id=old["id"], payload=payload
            )
            outcomes.append(
                CarryoverOutcome(
                    old["id"], CARRYOVER_BRANCH_B_HIT, ticket_id=ticket_id, reason="b 词法命中：疑似漏抽加急工单"
                )
            )
        else:
            _merge_meta(old_row, {"state": STATE_NEEDS_REVIEW, "rule": "carryover_b_miss"})
            outcomes.append(
                CarryoverOutcome(
                    old["id"], CARRYOVER_BRANCH_B_MISS, reason="b 词法未命中：确认删除转 needs_review（默认可见）"
                )
            )

    await session.flush()
    return CarryoverReport(outcomes=tuple(outcomes))


# ---------------------------------------------------------------- 冲突工单裁决执行（T2 人工裁决落库）


async def apply_decision(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    ticket: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> DecisionReport:
    """执行冲突工单人工裁决（§8.1 v1 裁决选项：点选胜者 / 判 T3 共存（人工填 scope）/ 待定）。

    ``ticket`` 为 ReviewTicketService.get_ticket 返回形（校验 target_type=conflict）；
    ``decision`` = {resolution, scope_a?, scope_b?, resolved_by?, comment?}。事实侧写路径：
    - 胜者：败者封口（meta valid_to=裁决时刻）+ outranked_by 边 + needs_review 标注
      （v1 简化为封口+标注即可，本实现连边保 §8.2 语义；败者不物理删除）；
    - t3_coexist：人工填 scope 写入双方 meta.scope（键须 ⊆ 封闭枚举 SCOPE_KEYS）；
    - pending：不动事实，仅回 payload_patch。
    工单状态推进（approved/published）归 review 决策端点，接线登记遗留——本函数返回
    ``payload_patch`` 供调用方合并回工单信封做裁决留痕（merge_payload 整体重赋值纪律）。
    """
    if ticket.get("target_type") != TARGET_TYPE_CONFLICT or ticket.get("tenant_id", tenant_id) != tenant_id:
        raise ValueError("3001 PARAM_INVALID: 非本租户冲突工单（target_type 须为 conflict）")
    resolution = str(decision.get("resolution") or "")
    if resolution not in DECISION_OPTIONS:
        raise ValueError(f"3001 PARAM_INVALID: resolution 仅支持 {'/'.join(DECISION_OPTIONS)}（收到 {resolution!r}）")
    inner = (ticket.get("payload") or {}).get("payload") or {}
    try:
        fact_a_id = uuid.UUID(str(inner.get("fact_a_id")))
        fact_b_id = uuid.UUID(str(inner.get("fact_b_id")))
    except (ValueError, TypeError) as exc:
        raise ConflictTriageError(f"404 工单信封缺事实对（fact_a_id/fact_b_id）: {ticket.get('id')}") from exc
    resolved_by = decision.get("resolved_by")
    comment = decision.get("comment")
    decided_at = datetime.now(UTC).isoformat()
    payload_patch = {
        "resolution": resolution,
        "resolved_by": str(resolved_by) if resolved_by else None,
        "comment": comment,
        "decided_at": decided_at,
    }

    if resolution == DECISION_PENDING:
        return DecisionReport(ticket["id"], resolution, payload_patch=payload_patch)

    if resolution in (DECISION_WINNER_A, DECISION_WINNER_B):
        winner_id, loser_id = (fact_a_id, fact_b_id) if resolution == DECISION_WINNER_A else (fact_b_id, fact_a_id)
        loser = await session.get(KbFact, loser_id)
        if loser is None or loser.tenant_id != tenant_id:
            raise ConflictTriageError(f"404 败者事实不存在: {loser_id}")
        _merge_meta(
            loser,
            {
                "state": STATE_OUTRANKED,
                "needs_review": True,  # v1 简化标注：败者默认可见待复核（遮蔽而非消失）
                "valid_to": decided_at,
                "winner_fact_id": str(winner_id),
                "resolution": resolution,
                "decided_by": str(resolved_by) if resolved_by else None,
            },
        )
        created = await _add_edge(
            session,
            tenant_id,
            loser_id,
            winner_id,
            RELATION_OUTRANKED_BY,
            {
                "rule": "t2_manual_decision",
                "resolution": resolution,
                "ticket_id": str(ticket["id"]),
                "decided_by": str(resolved_by) if resolved_by else None,
            },
        )
        return DecisionReport(
            ticket["id"],
            resolution,
            winner_fact_id=winner_id,
            loser_fact_id=loser_id,
            facts_annotated=1,
            edges_created=int(created),
            payload_patch=payload_patch,
        )

    # t3_coexist：人工判定限定共存，人工填 scope 写入双方（键须 ⊆ 封闭枚举，§8.1 scope 硬门禁）
    scope_a, scope_b = decision.get("scope_a"), decision.get("scope_b")
    if not (isinstance(scope_a, Mapping) and scope_a) or not (isinstance(scope_b, Mapping) and scope_b):
        raise ValueError("3001 PARAM_INVALID: t3_coexist 须人工填 scope_a/scope_b（非空）")
    for scope in (scope_a, scope_b):
        illegal = set(scope) - SCOPE_KEYS
        if illegal:
            raise ValueError(
                f"3001 PARAM_INVALID: scope 键越封闭枚举（{sorted(illegal)}，合法 = {sorted(SCOPE_KEYS)}）"
            )
    facts_annotated = 0
    for fid, scope in ((fact_a_id, scope_a), (fact_b_id, scope_b)):
        fact = await session.get(KbFact, fid)
        if fact is None or fact.tenant_id != tenant_id:
            raise ConflictTriageError(f"404 事实不存在: {fid}")
        meta = dict(fact.meta or {})
        meta["scope"] = dict(scope)
        fact.meta = meta
        decided_by = str(resolved_by) if resolved_by else None
        _merge_meta(fact, {"state": STATE_COEXIST, "resolution": resolution, "decided_by": decided_by})
        facts_annotated += 1
    return DecisionReport(
        ticket["id"],
        resolution,
        facts_annotated=facts_annotated,
        payload_patch=payload_patch,
    )
