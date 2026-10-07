"""OntologyRepository 的 L6 PG 实现 + 制品库（06 篇 §1 repo_impl 纪律）。

- 强制租户过滤：tenant_id 构造期绑定；防御跨租户写（session_repo 同款）；
- 发布事务序（本体核心设计 §4/§9）：**制品写成功 → PG 版本行**（调用方 UoW/会话事务边界内，
  任一失败整体回滚）；M2 制品落本地 `deploy/artifacts/` 目录（key 规范与 MinIO 一致：
  `ontologies/{tenant_id}/{ontology_id}/{version}.ttl`），TODO(M4)：切换 MinIO 对象存储（boto3，
  S3 协议）——put/get/discard 签名不变，调用方零改动；
- **Neo4j 物化 M2 跳过**（登记）：TBox 属性图为只读物化视图（n10s 异步刷新随 M3+，ontology §4），
  检索基线不依赖本物化——发布后物化刷新钩子留 TODO(M3)；
- 一致性不变式（§4）：ontology_versions.checksum == 制品哈希；Neo4j 侧版本标记随物化引入时对齐。
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from services.ontology.data.audit_actions import OntologyAuditAction, record_ontology_audit
from services.ontology.data.orm import Axiom as AxiomORM
from services.ontology.data.orm import OntoClass as OntoClassORM
from services.ontology.data.orm import Ontology as OntologyORM
from services.ontology.data.orm import OntologyChangeset as OntologyChangesetORM
from services.ontology.data.orm import OntologyVersion as OntologyVersionORM
from services.ontology.data.orm import OntoProperty as OntoPropertyORM
from services.ontology.data.orm import Rule as RuleORM
from services.ontology.domain.model.ontology import (
    ChangesetStatus,
    Ontology,
    OntologyChangeset,
    OntologyStatus,
    OntologyVersionRef,
)
from services.ontology.domain.model.ontology_read_model import (
    ReadModelProjection,
)

_DEFAULT_ARTIFACT_ROOT = Path("deploy/artifacts")

# 撤除=标记（ONT-1.3）：元素类型 → (读模型 ORM, 元素键列)。element_key 口径（DDL §ONT-1.1）：
# class/property=iri；rule=name；axiom 以行 id 定位（表无自身 IRI 列，row UUID 即撤除定位键）。
_ELEMENT_TABLES: dict[str, tuple[type, object]] = {
    "class": (OntoClassORM, OntoClassORM.iri),
    "property": (OntoPropertyORM, OntoPropertyORM.iri),
    "axiom": (AxiomORM, AxiomORM.id),
    "rule": (RuleORM, RuleORM.name),
}
_CANDIDATE_TABLES: dict[str, tuple[type, object]] = {
    "axiom": (AxiomORM, AxiomORM.subject_iri),  # 元素键口径（DDL）：axiom=iri（取 subject_iri）
    "rule": (RuleORM, RuleORM.name),
}


def _mark_fields(marks: dict, key) -> dict:
    """撤除标记 → ORM 构造 kwargs（未命中=在役 None/None，重投影携带 ONT-1.3 标记）。"""
    w_at, w_reason = marks.get(key, (None, None))
    return {"withdrawn_at": w_at, "withdrawn_reason": w_reason}


class VersionSummary(BaseModel):
    """版本历史摘要（详情/回滚用例定制查询，04 §4「查询方法按用例定制」）。"""

    version: str
    version_no: int
    artifact_key: str
    checksum: str
    created_at: datetime | None = None


class LocalArtifactStore:
    """制品库（权威制品=版本化 Turtle，ontology §4 MinIO 行）。

    M2 本地目录实现：put 返回 sha256（checksum 三方巡检锚点）；TODO(M4) 切 MinIO（boto3）。
    """

    def __init__(self, root: str | Path = _DEFAULT_ARTIFACT_ROOT) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def artifact_key(self, tenant_id: uuid.UUID, ontology_id: uuid.UUID, version: str) -> str:
        """key 规范（ontology §4）：ontologies/{tenant_id}/{ontology_id}/{version}.ttl。"""
        return f"ontologies/{tenant_id}/{ontology_id}/{version}.ttl"

    def put(self, key: str, content: bytes) -> str:
        """写入制品，返回 sha256 hex（先写制品后落版本行的发布事务第一步）。"""
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    def get(self, key: str) -> str:
        return (self._root / key).read_text(encoding="utf-8")

    def exists(self, key: str) -> bool:
        return (self._root / key).is_file()

    def discard(self, key: str) -> None:
        """孤儿制品清理（发布事务 PG 侧失败时尽力回收；M4 MinIO 同名接口）。"""
        (self._root / key).unlink(missing_ok=True)


def _now() -> datetime:
    return datetime.now(UTC)


def _changeset_to_domain(row: OntologyChangesetORM) -> OntologyChangeset:
    impact = row.impact_report or {}
    return OntologyChangeset(
        id=row.id,
        title=row.title or "",
        status=ChangesetStatus(row.status),
        target_key=row.target_key,
        gate_ok=bool(impact.get("gate_ok", False)),
        gate_report=impact.get("gate_report") or {},
        approvals=impact.get("approvals") or {},
        applicant_id=row.applicant_id,
        reviewer_id=row.reviewer_id,
        review_comment=row.review_comment,
        submitted_at=row.submitted_at,
        published_at=row.published_at,
    )


def _version_ref(row: OntologyVersionORM) -> OntologyVersionRef:
    return OntologyVersionRef(version=row.version, artifact_key=row.artifact_key, checksum=row.checksum)


class PgOntologyRepository:
    """OntologyRepository 的 PG 实现（签名见 services/domain/repo/ontology_repo.py）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID, artifacts: LocalArtifactStore | None = None) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._artifacts = artifacts or LocalArtifactStore()

    @property
    def artifacts(self) -> LocalArtifactStore:
        return self._artifacts

    async def get(self, ontology_id: uuid.UUID) -> Ontology | None:
        stmt = select(OntologyORM).where(OntologyORM.id == ontology_id, OntologyORM.tenant_id == self._tenant_id)
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        head_ref = None
        if row.current_version_id is not None:
            version_row = await self._db.get(OntologyVersionORM, row.current_version_id)
            if version_row is not None:
                head_ref = _version_ref(version_row)
        latest_changeset = await self._latest_changeset(ontology_id)
        return self._ontology_from_row(
            row,
            head_ref=head_ref,
            active_changeset=_changeset_to_domain(latest_changeset) if latest_changeset is not None else None,
        )

    async def save(self, ontology: Ontology) -> None:
        """全量保存聚合标量状态 + active_changeset 行 upsert（必须在 UoW/会话事务内调用）。"""
        row = await self._db.get(OntologyORM, ontology.id)
        if row is None:
            row = OntologyORM(id=ontology.id, tenant_id=ontology.tenant_id)
            self._db.add(row)
        if row.tenant_id != self._tenant_id:  # 防御：禁止跨租户写（06 篇 §1 repo_impl 纪律）
            raise ValueError("租户不匹配：拒绝写入他租户本体行")
        row.iri_base = ontology.iri_base
        row.name = ontology.name
        row.description = ontology.description
        row.scheme_tier = ontology.scheme_tier
        row.status = ontology.status.value
        row.current_version_id = await self._version_row_id(ontology.id, ontology.head_version)
        if ontology.active_changeset is not None:
            await self._upsert_changeset(ontology.id, ontology.active_changeset)
        await self._db.flush()

    async def list(self, *, status: OntologyStatus | None = None, offset: int = 0, limit: int = 20) -> list[Ontology]:
        stmt = select(OntologyORM).where(OntologyORM.tenant_id == self._tenant_id)
        if status is not None:
            stmt = stmt.where(OntologyORM.status == status.value)
        stmt = stmt.order_by(OntologyORM.created_at.desc(), OntologyORM.id.desc()).offset(offset).limit(limit)
        rows = (await self._db.execute(stmt)).scalars().all()
        if not rows:
            return []
        ids = [r.id for r in rows]
        head_map = await self._head_map(ids)
        changeset_map = await self._latest_changeset_map(ids)
        return [
            self._ontology_from_row(r, head_ref=head_map.get(r.id), active_changeset=changeset_map.get(r.id))
            for r in rows
        ]

    async def search(self, query: str, *, limit: int = 20) -> list[Ontology]:
        """本体搜索（api/01 §5.3 search 行 M2 最小闭环）：名称/描述/命名空间 IRI 片段不区分大小写匹配。

        语义检索（Milvus 向量，ontology §7.2）随 M3+ 检索面接入替换本实现，签名不变；
        ILIKE 通配符转义（%/_）防片段语义被通配符旁路。
        """
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        stmt = (
            select(OntologyORM)
            .where(
                OntologyORM.tenant_id == self._tenant_id,
                (OntologyORM.name.ilike(pattern, escape="\\"))
                | (OntologyORM.iri_base.ilike(pattern, escape="\\"))
                | (OntologyORM.description.ilike(pattern, escape="\\")),
            )
            .order_by(OntologyORM.created_at.desc(), OntologyORM.id.desc())
            .limit(limit)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        if not rows:
            return []
        head_map = await self._head_map([r.id for r in rows])
        return [self._ontology_from_row(r, head_ref=head_map.get(r.id), active_changeset=None) for r in rows]

    async def append_version(
        self,
        ontology_id: uuid.UUID,
        *,
        content: str,
        changelog: str | None = None,
        published_by: uuid.UUID | None = None,
    ) -> OntologyVersionRef:
        """发布事务（本体核心设计 §9）：制品写成功 → PG 版本行；返回新 head 指针。

        制品为本地文件无法随 PG 回滚：PG 侧插入失败即 discard 孤儿文件；事务提交阶段失败
        的孤儿由每日巡检对账（checksum 三方巡检，ontology §4），M4 切 MinIO 后同策略。
        """
        current_no = (
            await self._db.execute(
                select(func.max(OntologyVersionORM.version_no)).where(
                    OntologyVersionORM.ontology_id == ontology_id, OntologyVersionORM.tenant_id == self._tenant_id
                )
            )
        ).scalar_one()
        version_no = 1 if current_no is None else int(current_no) + 1
        version = f"v{version_no}"
        parent_row = await self._current_version_row(ontology_id)
        key = self._artifacts.artifact_key(self._tenant_id, ontology_id, version)
        checksum = self._artifacts.put(key, content.encode("utf-8"))  # 第一步：制品写成功
        try:  # 第二步：PG 版本行（同事务；失败整体回滚）
            row = OntologyVersionORM(
                tenant_id=self._tenant_id,
                ontology_id=ontology_id,
                version=version,
                version_no=version_no,
                artifact_key=key,
                checksum=checksum,
                changelog=changelog,
                parent_version_id=parent_row.id if parent_row is not None else None,
                published_by=published_by,
                published_at=_now(),
            )
            self._db.add(row)
            await self._db.flush()
        except SQLAlchemyError:
            self._artifacts.discard(key)
            raise
        return OntologyVersionRef(version=version, artifact_key=key, checksum=checksum)

    async def read_artifact(self, artifact_key: str) -> str:
        """读制品内容（同步磁盘 IO → to_thread；缺失抛 FileNotFoundError，调用方转 4201）。"""
        return await asyncio.to_thread(self._artifacts.get, artifact_key)

    async def replace_read_model(
        self,
        ontology_id: uuid.UUID,
        *,
        version: str,
        changeset_id: uuid.UUID | None,
        projection: ReadModelProjection,
    ) -> None:
        """发布读模型投影替换写（database/01 §3.3 四表；必须在调用方 UoW/会话事务内调用）。

        同 ontology+version 先删后插（幂等重放）；与调用方同一会话 → 投影与版本行/聚合状态
        同一 PG 事务（单事务边界，本体核心设计 §9）。`version` 为版本标识，先解析版本行 id。
        """
        version_id = await self._version_id_by_name(ontology_id, version)
        if version_id is None:
            raise ValueError(f"版本行不存在: {ontology_id}/{version}（投影必须在 append_version 同事务之后调用，§9）")
        common = {"tenant_id": self._tenant_id, "ontology_id": ontology_id, "version_id": version_id}
        # ONT-1.3 撤除标记携带：同版本幂等重放（先删后插）永不洗掉 withdrawn 标记——删除前
        # 先读标记，重插时回填（类/属性按 iri、规则按 name、公理按 kind+subject+expression 定位）。
        class_marks = await self._withdrawn_marks(OntoClassORM.iri, ontology_id, version_id)
        property_marks = await self._withdrawn_marks(OntoPropertyORM.iri, ontology_id, version_id)
        rule_marks = await self._withdrawn_marks(RuleORM.name, ontology_id, version_id)
        axiom_marks: dict[tuple[str, str, str], tuple[datetime | None, str | None]] = {}
        for a_iri, a_kind, a_expr, a_at, a_reason in (
            (
                await self._db.execute(
                    select(AxiomORM.subject_iri, AxiomORM.kind, AxiomORM.expression,
                           AxiomORM.withdrawn_at, AxiomORM.withdrawn_reason).where(
                        AxiomORM.tenant_id == self._tenant_id,
                        AxiomORM.ontology_id == ontology_id,
                        AxiomORM.version_id == version_id,
                        AxiomORM.withdrawn_at.is_not(None),
                    )
                )
            )
            .all()
        ):
            axiom_marks[(a_kind, a_iri, a_expr)] = (a_at, a_reason)
        # 先删（替换式投影；uk_*_version_id_iri 唯一约束由先删后插保证）
        for orm in (OntoClassORM, OntoPropertyORM, AxiomORM, RuleORM):
            await self._db.execute(
                delete(orm).where(
                    orm.tenant_id == self._tenant_id, orm.ontology_id == ontology_id, orm.version_id == version_id
                )
            )
        self._db.add_all(
            [
                OntoClassORM(
                    **common,
                    changeset_id=changeset_id,
                    iri=c.iri,
                    name=c.name,
                    label=c.label,
                    definition=c.definition,
                    subclass_of=c.subclass_of,
                    equivalent_class=c.equivalent_class,
                    is_behavior=c.is_behavior,
                    state_attribute=c.state_attribute,
                    metadata_=c.metadata,
                    **_mark_fields(class_marks, c.iri),
                )
                for c in projection.classes
            ]
        )
        self._db.add_all(
            [
                OntoPropertyORM(
                    **common,
                    changeset_id=changeset_id,
                    iri=p.iri,
                    kind=p.kind,
                    name=p.name,
                    label=p.label,
                    definition=p.definition,
                    domain_iri=p.domain_iri,
                    range_iri=p.range_iri,
                    type_of_terms=p.type_of_terms,
                    functional=p.functional,
                    constraints=p.constraints,
                    metadata_=p.metadata,
                    **_mark_fields(property_marks, p.iri),
                )
                for p in projection.properties
            ]
        )
        self._db.add_all(
            [
                AxiomORM(
                    **common,
                    changeset_id=changeset_id,
                    kind=a.kind,
                    subject_iri=a.subject_iri,
                    object_iri=a.object_iri,
                    expression=a.expression,
                    source=a.source,
                    review_state=a.review_state,
                    **_mark_fields(axiom_marks, (a.kind, a.subject_iri, a.expression)),
                )
                for a in projection.axioms
            ]
        )
        self._db.add_all(
            [
                RuleORM(
                    **common,
                    changeset_id=changeset_id,
                    route=r.route,
                    name=r.name,
                    description=r.description,
                    event_class_iri=r.event_class_iri,
                    condition=r.condition,
                    action_ref=r.action_ref,
                    severity=r.severity,
                    enabled=r.enabled,
                    source=r.source,
                    review_state=r.review_state,
                    **_mark_fields(rule_marks, r.name),
                )
                for r in projection.rules
            ]
        )
        await self._db.flush()

    async def get_version(self, ontology_id: uuid.UUID, version: str) -> OntologyVersionRef | None:
        stmt = select(OntologyVersionORM).where(
            OntologyVersionORM.ontology_id == ontology_id,
            OntologyVersionORM.tenant_id == self._tenant_id,
            OntologyVersionORM.version == version,
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        return _version_ref(row) if row is not None else None

    async def list_versions(self, ontology_id: uuid.UUID, *, limit: int = 100) -> list[VersionSummary]:
        """版本历史（version_no 降序；回滚用例取 version_no 小于当前 head 的最近一条）。"""
        stmt = (
            select(OntologyVersionORM)
            .where(OntologyVersionORM.ontology_id == ontology_id, OntologyVersionORM.tenant_id == self._tenant_id)
            .order_by(OntologyVersionORM.version_no.desc())
            .limit(limit)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return [
            VersionSummary(
                version=r.version,
                version_no=r.version_no,
                artifact_key=r.artifact_key,
                checksum=r.checksum,
                created_at=r.created_at,
            )
            for r in rows
        ]

    # ---- ONT-1：审计通道 / usage 守卫 / 撤除=标记 / 候选拒绝（06 篇 §ONT-1.3~1.6） ----

    async def record_audit(
        self,
        *,
        actor_id: uuid.UUID | None,
        action: OntologyAuditAction | str,
        ontology_id: uuid.UUID,
        digest: dict,
        trace_id: str | None = None,
        subject: str = "ontology",
        subject_id: uuid.UUID | None = None,
    ) -> None:
        """ontology.* 审计行（audit_logs；同事务，tools record_audit 先例）。"""
        await record_ontology_audit(
            self._db,
            tenant_id=self._tenant_id,
            action=action,
            actor_id=actor_id,
            ontology_id=ontology_id,
            digest=digest,
            trace_id=trace_id,
            subject=subject,
            subject_id=subject_id,
        )

    async def kb_usage_count(self, element_type: str, iri: str) -> int:
        """kb 域对该 IRI 的**全历史**引用计数（ONT-1.4 第 1 档 usage 守卫计数面）。

        不筛 status（candidate+rejected+authoritative=全历史，含历史版本行）；计数面：
        类 IRI=kb_facts.subject_type/object_type + kb_rule_candidates.target_class；
        属性 IRI=kb_facts.predicate（subject/object 为 ABox 实体 IRI，不入类/属性计数）。
        裸 SQL 跨模块查询（免 kb ORM import 边，tools→audit_logs 同款）。
        """
        if element_type == "class":
            facts = await self._db.execute(
                text(
                    "SELECT count(*) FROM kb_facts WHERE tenant_id = CAST(:t AS uuid) "
                    "AND (subject_type = :iri OR object_type = :iri)"
                ),
                {"t": str(self._tenant_id), "iri": iri},
            )
            rules = await self._db.execute(
                text(
                    "SELECT count(*) FROM kb_rule_candidates WHERE tenant_id = CAST(:t AS uuid) "
                    "AND target_class = :iri"
                ),
                {"t": str(self._tenant_id), "iri": iri},
            )
            return int(facts.scalar_one()) + int(rules.scalar_one())
        if element_type == "property":
            facts = await self._db.execute(
                text(
                    "SELECT count(*) FROM kb_facts WHERE tenant_id = CAST(:t AS uuid) AND predicate = :iri"
                ),
                {"t": str(self._tenant_id), "iri": iri},
            )
            return int(facts.scalar_one())
        raise ValueError(f"usage 守卫仅适用 class/property（收到 {element_type}；rule/axiom 撤除无守卫）")

    async def withdraw_head_element(
        self, ontology_id: uuid.UUID, *, version: str, element_type: str, key: str, reason: str
    ) -> int:
        """撤除=标记（ONT-1.3）：head 版本行打 withdrawn_at/withdrawn_reason，永不物理删。

        返回标记行数（0=元素不存在）；已撤除行不重复打戳（幂等重放 rowcount=0）。
        axiom 以行 id 定位（_ELEMENT_TABLES 口径），非法 UUID ValueError 由调用方转 4xx。
        """
        try:
            orm, key_col = _ELEMENT_TABLES[element_type]
        except KeyError as exc:
            raise ValueError(f"未知元素类型: {element_type}（白名单 {'|'.join(_ELEMENT_TABLES)}）") from exc
        version_id = await self._version_id_by_name(ontology_id, version)
        if version_id is None:
            raise ValueError(f"版本行不存在: {ontology_id}/{version}")
        key_val: object = uuid.UUID(key) if element_type == "axiom" else key
        result = await self._db.execute(
            update(orm)
            .where(
                orm.tenant_id == self._tenant_id,
                orm.ontology_id == ontology_id,
                orm.version_id == version_id,
                key_col == key_val,
                orm.withdrawn_at.is_(None),
            )
            .values(withdrawn_at=_now(), withdrawn_reason=reason)
        )
        return int(result.rowcount or 0)

    async def decline_candidate(
        self,
        ontology_id: uuid.UUID,
        *,
        element_type: str,
        row_id: uuid.UUID,
        reason: str,
        actor_id: uuid.UUID | None,
        trace_id: str | None = None,
    ) -> bool:
        """候选拒绝（防重提层三，ONT-1.3/1.6）：仅 source='llm_candidate' 行可打 declined_reason。

        同事务审计 ontology.proposal.declined；非候选行/不存在 → False（调用方转 409，4202 码）。
        """
        try:
            orm, _key_col = _CANDIDATE_TABLES[element_type]
        except KeyError as exc:
            raise ValueError(f"候选仅存在 axiom/rule（收到 {element_type}）") from exc
        result = await self._db.execute(
            update(orm)
            .where(
                orm.tenant_id == self._tenant_id,
                orm.ontology_id == ontology_id,
                orm.id == row_id,
                orm.source == "llm_candidate",
                orm.declined_reason.is_(None),
            )
            .values(declined_reason=reason)
        )
        declined = int(result.rowcount or 0) > 0
        if declined:
            await self.record_audit(
                actor_id=actor_id,
                action=OntologyAuditAction.PROPOSAL_DECLINED,
                ontology_id=ontology_id,
                digest={"element_type": element_type, "row_id": str(row_id), "reason": reason},
                trace_id=trace_id,
            )
        return declined

    async def declined_evidence_floor(
        self, ontology_id: uuid.UUID, *, element_type: str, element_key: str
    ) -> int | None:
        """同形候选再提的证据量下限（ONT-1.3 翻倍判据）：declined 历史 max(evidence_count)×2。

        element_key 口径：axiom=subject_iri；rule=name。无 declined 历史 → None（首提不受限）。
        """
        try:
            orm, key_col = _CANDIDATE_TABLES[element_type]
        except KeyError as exc:
            raise ValueError(f"候选仅存在 axiom/rule（收到 {element_type}）") from exc
        current_max = (
            await self._db.execute(
                select(func.max(orm.evidence_count)).where(
                    orm.tenant_id == self._tenant_id,
                    orm.ontology_id == ontology_id,
                    key_col == element_key,
                    orm.source == "llm_candidate",
                    orm.declined_reason.is_not(None),
                )
            )
        ).scalar_one()
        return None if current_max is None else 2 * int(current_max)

    async def _withdrawn_marks(
        self, key_col, ontology_id: uuid.UUID, version_id: uuid.UUID
    ) -> dict:
        """撤除标记快照（重投影携带用）：key → (withdrawn_at, withdrawn_reason)，仅已撤除行。"""
        orm = key_col.class_
        rows = (
            await self._db.execute(
                select(key_col, orm.withdrawn_at, orm.withdrawn_reason).where(
                    orm.tenant_id == self._tenant_id,
                    orm.ontology_id == ontology_id,
                    orm.version_id == version_id,
                    orm.withdrawn_at.is_not(None),
                )
            )
        ).all()
        return {key: (w_at, w_reason) for key, w_at, w_reason in rows}

    # ---- 内部装配 ----

    async def _version_id_by_name(self, ontology_id: uuid.UUID, version: str) -> uuid.UUID | None:
        """版本标识 → 版本行 id（读模型投影挂靠列 version_id 的解析）。"""
        stmt = select(OntologyVersionORM.id).where(
            OntologyVersionORM.ontology_id == ontology_id,
            OntologyVersionORM.tenant_id == self._tenant_id,
            OntologyVersionORM.version == version,
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def _latest_changeset(self, ontology_id: uuid.UUID) -> OntologyChangesetORM | None:
        stmt = (
            select(OntologyChangesetORM)
            .where(
                OntologyChangesetORM.ontology_id == ontology_id,
                OntologyChangesetORM.tenant_id == self._tenant_id,
            )
            .order_by(OntologyChangesetORM.created_at.desc(), OntologyChangesetORM.id.desc())
            .limit(1)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def _latest_changeset_map(self, ontology_ids: list[uuid.UUID]) -> dict[uuid.UUID, OntologyChangeset]:
        stmt = (
            select(OntologyChangesetORM)
            .where(
                OntologyChangesetORM.ontology_id.in_(ontology_ids),
                OntologyChangesetORM.tenant_id == self._tenant_id,
            )
            .order_by(OntologyChangesetORM.created_at.desc(), OntologyChangesetORM.id.desc())
        )
        result: dict[uuid.UUID, OntologyChangeset] = {}
        for row in (await self._db.execute(stmt)).scalars():
            result.setdefault(row.ontology_id, _changeset_to_domain(row))  # 首条即最新（按时间降序遍历）
        return result

    def _ontology_from_row(
        self,
        row: OntologyORM,
        *,
        head_ref: OntologyVersionRef | None,
        active_changeset: OntologyChangeset | None,
    ) -> Ontology:
        """ORM 行 → 聚合（get/list/search 三站点单一映射源，防字段增删三处漂移）。"""
        return Ontology(
            id=row.id,
            tenant_id=row.tenant_id,
            iri_base=row.iri_base,
            name=row.name,
            description=row.description,
            scheme_tier=row.scheme_tier,
            status=OntologyStatus(row.status),
            head_version=head_ref,
            active_changeset=active_changeset,
        )

    async def _head_map(self, ontology_ids: list[uuid.UUID]) -> dict[uuid.UUID, OntologyVersionRef]:
        stmt = select(OntologyORM.id, OntologyVersionORM).join(
            OntologyVersionORM,
            OntologyVersionORM.id == OntologyORM.current_version_id,
        )
        stmt = stmt.where(OntologyORM.id.in_(ontology_ids), OntologyORM.tenant_id == self._tenant_id)
        return {oid: _version_ref(row) for oid, row in (await self._db.execute(stmt)).all()}

    async def _version_row_id(self, ontology_id: uuid.UUID, head: OntologyVersionRef | None) -> uuid.UUID | None:
        if head is None:
            return None
        stmt = select(OntologyVersionORM.id).where(
            OntologyVersionORM.ontology_id == ontology_id,
            OntologyVersionORM.tenant_id == self._tenant_id,
            OntologyVersionORM.version == head.version,
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def _current_version_row(self, ontology_id: uuid.UUID) -> OntologyVersionORM | None:
        stmt = select(OntologyORM.current_version_id).where(
            OntologyORM.id == ontology_id, OntologyORM.tenant_id == self._tenant_id
        )
        current_id = (await self._db.execute(stmt)).scalar_one_or_none()
        return await self._db.get(OntologyVersionORM, current_id) if current_id is not None else None

    async def _upsert_changeset(self, ontology_id: uuid.UUID, changeset: OntologyChangeset) -> None:
        row = await self._db.get(OntologyChangesetORM, changeset.id)
        if row is None:
            row = OntologyChangesetORM(
                id=changeset.id,
                tenant_id=self._tenant_id,
                ontology_id=ontology_id,
                title=changeset.title,
                status=changeset.status.value,
                target_key=changeset.target_key,
            )
            self._db.add(row)
        if row.tenant_id != self._tenant_id:
            raise ValueError("租户不匹配：拒绝写入他租户变更单行")
        # 终态行照常 upsert（历史归档只读）；同本体至多一个活跃行由 uk_changesets_one_active
        # 部分唯一索引兜底（并发型不变式归存储，04 篇 §2.1），内存单活跃裁决在聚合方法内；
        # 同目标活跃单不双开由 uk_changesets_one_target 兜底（ONT-1.5，冲突→IntegrityError→409）。
        row.title = changeset.title
        row.status = changeset.status.value
        row.target_key = changeset.target_key
        row.applicant_id = changeset.applicant_id
        row.reviewer_id = changeset.reviewer_id
        row.review_comment = changeset.review_comment
        row.submitted_at = changeset.submitted_at
        row.published_at = changeset.published_at
        row.impact_report = {
            "gate_ok": changeset.gate_ok,
            "gate_report": changeset.gate_report,
            "approvals": changeset.approvals,
        }
