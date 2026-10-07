"""kb nightly 保鲜例程 v1（OntRAG 知识库GraphRAG设计 §8.4 分期收缩范围，2026-09-27 分期表）。

v1 三件事：
- ① 催办：candidate 候选 created_at 满 overdue_days（默认 14）→ 按 (tenant, subject, subject_type)
  聚合入报告（§7.1 批次纪律同款分组、最旧组优先），v1 只报告不改数据（催办通知面随工作台切片）；
- ② 超时归档：created_at 满 archive_days（默认 30）逐条处置，单条计动作；max_actions 为单次
  运行硬预算，超限截断（确定性序 created_at,id——先到先处置）顺延次夜，budget_triggered 置位；
  催办为只读不占预算（动作=状态变更，口径见 §8.4 预算纪律）；
- ③ 悬空出处：evidence.source_ref 四元组指向不存在的 document / chunk（两类，纯廉价项）→
  报告 issues；扫描面=live 词汇（candidate/authoritative），rejected（含本轮刚归档者）不再巡检。
- ④ 零引用清理候选（多源接入 §6.1 v1 知识活性反馈环消费面，2026-10-04 A3 激活批）：live chunk
  created_at 严格超 zero_ref_days（默认 90；恰满不计=清理候选宁保守）且 usage 表缺行（从未被
  检索引用）或 search_hits=0（全零）→ 计数入 stats.zero_ref_candidates；**v1 只报告不动数据**
  （删除动作与评审流随 v1.5；kb_usage_counters 不可用=观测缺失，置 -1 不拖垮例程）。

lite 取舍（kb_facts.status CheckConstraint 仅 candidate|rejected|authoritative，无 archived 枚举，
database/01 §3.3 v1 权威）：处置 = status 就近映射 rejected + meta.maintenance={'action':
'auto_archived', ...} 行内审计。rejected 使事实即刻退出复核队列与巡检面（§8.4「不再占队列」——
仅 meta 标记、status 留 candidate 无法满足此点）；meta 痕区分自动归档与人工驳回（「保留可溯」），
后续迁移补 archived 枚举时可按 meta 一次 SQL 重映射。

幂等（重跑收敛）：归档翻转 status 后即退出全部选择集——同 now 重跑 archived=0、reminded/issues
与首轮剩余状态一致，不双写不重复计数；中途失败单事务整批回滚，重跑幂等重处置。

调度互斥（§8.4 工程纪律 3 的 v1 最小实现）：MaintenanceLock 端口注入；PostgresAdvisoryLock =
pg_try_advisory_lock 会话级锁（键名对应设计 Redis 锁 lock:kb_nightly；连接断开自动释放，免
TTL/心跳续期）。lock 缺省 None = 单实例部署直跑；分布式部署由调度装配点传入端口实现
（Redis 锁随定时挂点切片接入，端口已就位可替换）。SQLite 无此函数，测试以桩锁注入。

定时挂点（2026-09-29 核查）：services/platform/ 现无 worker/定时任务注册面（arq/celery/
apscheduler 全仓零命中），本函数即交付面——外部调度器（L3 任务 kb_nightly_maintenance）直调
并装配锁端口，挂点待接。

时间口径：now 与库内 created_at 必须同一时区表述（PG=aware UTC；SQLite 测试=naive 同值往返），
本函数不做隐式转换。stats 记账（§8.4 工程纪律 2 的 v1 形态）随报告返回；kb_maintenance_runs
水位 checkpoint 属增量重编译（v1.5）范围，本切片不含。
"""

from __future__ import annotations

import logging
import uuid
import zlib
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.kb.data.orm import Document, DocumentChunk, KbFact

logger = logging.getLogger(__name__)

# v1 复核队列词汇=candidate（needs_review 为 §8.1 b 类目标态，落库迁移另切片，review_queue 同结论）
_QUEUE_STATUS = "candidate"
# 悬空出处扫描面=live 词汇：rejected（人工驳回与自动归档）已出环流，不再巡检
_LIVE_SCAN_STATUSES: tuple[str, ...] = ("candidate", "authoritative")
_IN_BATCH = 500  # 存在性批量回查批大小（IN 列表上限，防巨型绑定）
_AUTO_ARCHIVED = "auto_archived"  # meta.maintenance.action 标记值（lite 取舍见模块头）
_ADVISORY_KEY = zlib.crc32(b"lock:kb_nightly")  # 设计 Redis 锁 lock:kb_nightly 的 DB 键映射（稳定）

__all__ = [
    "MaintenanceArchived",
    "MaintenanceIssue",
    "MaintenanceLock",
    "MaintenanceReminder",
    "MaintenanceReport",
    "PostgresAdvisoryLock",
    "run_kb_maintenance",
]


# ---------------------------------------------------------------- 报告载荷（business 只数据无行为，api/调度面投影 DTO）


class MaintenanceReminder(BaseModel):
    """催办聚合组（v1 只报告）：同租户同 subject 的逾期候选批次（§7.1 审核单元）。"""

    tenant_id: uuid.UUID
    subject: str
    subject_type: str | None
    count: int
    oldest_created_at: datetime  # 组内最旧候选（聚合排序键：最旧组优先）


class MaintenanceArchived(BaseModel):
    """单条归档留痕（rejected+meta.maintenance 双痕；回报告供审计对账）。"""

    fact_id: uuid.UUID
    tenant_id: uuid.UUID
    subject: str
    created_at: datetime


class MaintenanceIssue(BaseModel):
    """悬空出处告警：kind ∈ missing_document / missing_chunk（source_ref 指向不存在对象）。"""

    fact_id: uuid.UUID
    tenant_id: uuid.UUID
    kind: Literal["missing_document", "missing_chunk"]
    ref: str  # 悬空引用原值（含不可解析串——解析失败按悬空报）
    subject: str  # 分诊回指（免二次查行）


class MaintenanceReport(BaseModel):
    """一次例程运行的全部产出（frozen：可安全跨协程传递与日志落档）。"""

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    now: datetime  # 注入的运行时点（报告内不含墙钟，同 now 重跑报告逐字段可比对）
    reminded: list[MaintenanceReminder] = Field(default_factory=list)
    archived: list[MaintenanceArchived] = Field(default_factory=list)
    issues: list[MaintenanceIssue] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)  # 记账：candidates_scanned/remind_*/archived/issues_*
    budget_triggered: bool = False  # max_actions 截断发生（剩余顺延次夜）
    skipped: bool = False  # 锁忙未运行（防双跑）
    skip_reason: str | None = None


# ---------------------------------------------------------------- 锁端口（§8.4 工程纪律 3）


class MaintenanceLock(Protocol):
    """例程互斥端口：acquire False = 他 worker 在跑，本次跳过（防双跑）。"""

    async def acquire(self) -> bool: ...

    async def release(self) -> None: ...


class PostgresAdvisoryLock:
    """PG 会话级咨询锁（Redis 缺位时的 DB 锁最小实现；设计锁名 lock:kb_nightly 的 DB 版）。

    pg_try_advisory_lock 非阻塞拿锁，锁随连接存活——进程崩溃连接断开自动释放，免 TTL/心跳
    续期。仅 PG 方言可用（SQLite 无此函数：测试经 MaintenanceLock 端口注入桩锁）。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, key: int = _ADVISORY_KEY) -> None:
        self._factory = session_factory
        self._key = key
        self._conn: Any = None

    async def acquire(self) -> bool:
        engine = self._factory.kw["bind"]
        conn = await engine.connect()
        try:
            ok = bool((await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key})).scalar())
        except BaseException:
            await conn.close()
            raise
        if not ok:
            await conn.close()
            return False
        self._conn = conn
        return True

    async def release(self) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self._key})
        finally:
            await self._conn.close()
            self._conn = None


# ---------------------------------------------------------------- 例程主体


async def run_kb_maintenance(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    overdue_days: int = 14,
    archive_days: int = 30,
    max_actions: int = 500,
    zero_ref_days: int = 90,
    lock: MaintenanceLock | None = None,
) -> MaintenanceReport:
    """夜检四件事：催办聚合（只报告）→ 超时归档（计动作/预算截断）→ 悬空出处告警 → 零引用清理候选（只报告）。

    - 催办带 = [now-overdue_days, now-archive_days)：满 14 天即催办；达归档线不再催办，
      转入 ② 处置（一条候选任一时刻至多出现在一个面，报告不重复计数）；
    - 归档 = created_at ≤ now-archive_days，按 (created_at, id) 序处置至多 max_actions 条；
    - 零引用清理候选 = live chunk created_at ≤ now-zero_ref_days 且 usage 表缺行/全零（§6.1），
      v1 只报告计数（stats.zero_ref_candidates），不改任何数据；
    - lock 注入时未抢到 → skipped 报告原样返回（零写零扫）；抢到则 finally 必释放；
    - 幂等：同 now 重跑状态收敛（见模块头）——archived 幂等空、reminded/issues 复现。
    """
    if overdue_days < 0 or archive_days <= 0 or archive_days <= overdue_days:
        raise ValueError("3001 PARAM_INVALID: 需 0 ≤ overdue_days < archive_days（催办带必须先于归档线）")

    run_id = uuid.uuid4()
    if lock is not None and not await lock.acquire():
        logger.info("kb nightly maintenance skipped: lock busy (lock:kb_nightly)")
        return MaintenanceReport(
            run_id=run_id,
            now=now,
            skipped=True,
            skip_reason="lock_busy: 他 worker 持有 lock:kb_nightly，本次跳过（防双跑）",
        )
    try:
        return await _run(
            session_factory,
            run_id=run_id,
            now=now,
            overdue_days=overdue_days,
            archive_days=archive_days,
            max_actions=max_actions,
            zero_ref_days=zero_ref_days,
        )
    finally:
        if lock is not None:
            await lock.release()


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: uuid.UUID,
    now: datetime,
    overdue_days: int,
    archive_days: int,
    max_actions: int,
    zero_ref_days: int,
) -> MaintenanceReport:
    archive_cutoff = now - timedelta(days=archive_days)
    remind_cutoff = now - timedelta(days=overdue_days)

    async with session_factory() as session:
        # ── 记账基线：扫描时候选总量（先于归档翻转）────────────────────────
        candidates_scanned = int(
            (
                await session.execute(select(func.count()).select_from(KbFact).where(KbFact.status == _QUEUE_STATUS))
            ).scalar_one()
        )

        # ── ① 催办聚合（v1 只报告；窗口函数单 SQL：组计数 + 组内最旧行，免 N+1）──
        partition = (KbFact.tenant_id, KbFact.subject, KbFact.subject_type)
        group_count = func.count().over(partition_by=partition).label("grp_count")
        row_number = func.row_number().over(partition_by=partition, order_by=(KbFact.created_at, KbFact.id)).label("rn")
        remind_rows = (
            await session.execute(
                select(KbFact.tenant_id, KbFact.subject, KbFact.subject_type, KbFact.created_at)
                .add_columns(group_count, row_number)
                .where(
                    KbFact.status == _QUEUE_STATUS,
                    KbFact.created_at <= remind_cutoff,  # 满 overdue_days 即催办（含当日边界）
                    KbFact.created_at > archive_cutoff,  # 达归档线不催办——转 ② 处置
                )
                .order_by(KbFact.created_at, KbFact.id)
            )
        ).all()
        groups: dict[tuple[uuid.UUID, str, str | None], MaintenanceReminder] = {}
        remind_facts = 0
        for row in remind_rows:
            remind_facts += 1
            if row.rn == 1:  # 组首行携带组最旧时间与组计数（rn>1 行仅计入 facts 总数）
                groups[(row.tenant_id, row.subject, row.subject_type)] = MaintenanceReminder(
                    tenant_id=row.tenant_id,
                    subject=row.subject,
                    subject_type=row.subject_type,
                    count=int(row.grp_count),
                    oldest_created_at=row.created_at,
                )
        reminded = sorted(groups.values(), key=lambda g: (g.oldest_created_at, g.tenant_id, g.subject))

        # ── ② 超时归档（单事务整批：预算截断即批次边界；失败全回滚重跑幂等）──
        eligible = (
            (
                await session.execute(
                    select(KbFact)
                    .where(KbFact.status == _QUEUE_STATUS, KbFact.created_at <= archive_cutoff)
                    .order_by(KbFact.created_at, KbFact.id)  # 确定性序：先到先处置，截断顺延可复现
                    .with_for_update()  # PG 行锁防并发双处置（SQLite 无 op 静默忽略，review_queue 同款）
                    .limit(max_actions + 1)  # +1 探溢出：恰 max_actions 条不置预算位
                )
            )
            .scalars()
            .all()
        )
        budget_triggered = len(eligible) > max_actions
        archived: list[MaintenanceArchived] = []
        for fact in eligible[:max_actions]:
            fact.status = "rejected"  # 就近映射（lite 取舍见模块头：无 archived 枚举）
            meta = dict(fact.meta or {})  # JSONB 不可原地变更：整体重赋值（既有键不触碰）
            meta["maintenance"] = {
                "action": _AUTO_ARCHIVED,
                "archived_at": now.isoformat(),
                "run_id": str(run_id),
            }
            fact.meta = meta
            archived.append(
                MaintenanceArchived(
                    fact_id=fact.id, tenant_id=fact.tenant_id, subject=fact.subject, created_at=fact.created_at
                )
            )
        await session.commit()

        # ── ③ 悬空出处扫描（live 词汇；distinct 引用集分批回查存在性，免全表装载）──
        live_rows = (
            await session.execute(
                select(KbFact.id, KbFact.tenant_id, KbFact.subject, KbFact.evidence)
                .where(KbFact.status.in_(_LIVE_SCAN_STATUSES))
                .order_by(KbFact.id)  # 终序：issues 序稳定，同 now 重跑逐条可比对
            )
        ).all()
        doc_refs: set[str] = set()
        chunk_refs: set[str] = set()
        for row in live_rows:
            ref = (row.evidence or {}).get("source_ref") or {}
            if not isinstance(ref, dict):
                continue  # 空证据/异形信封：无引用可悬空，跳过（口径=两类告警，不另设 kind）
            d, c = ref.get("document_id"), ref.get("chunk_id")
            if isinstance(d, str) and d:
                doc_refs.add(d)
            if isinstance(c, str) and c:
                chunk_refs.add(c)
        existing_docs = await _existing_refs(session, Document.id, doc_refs)
        existing_chunks = await _existing_refs(session, DocumentChunk.id, chunk_refs)
        issues: list[MaintenanceIssue] = []
        for row in live_rows:
            ref = (row.evidence or {}).get("source_ref") or {}
            if not isinstance(ref, dict):
                continue
            d, c = ref.get("document_id"), ref.get("chunk_id")
            if isinstance(d, str) and d and d.lower() not in existing_docs:
                issues.append(
                    MaintenanceIssue(
                        fact_id=row.id, tenant_id=row.tenant_id, kind="missing_document", ref=d, subject=row.subject
                    )
                )
            if isinstance(c, str) and c and c.lower() not in existing_chunks:
                issues.append(
                    MaintenanceIssue(
                        fact_id=row.id, tenant_id=row.tenant_id, kind="missing_chunk", ref=c, subject=row.subject
                    )
                )

        # ── ④ 零引用清理候选（多源接入 §6.1 v1；悬空出处扫描步旁的活性反馈环消费面）──
        # live chunk created_at 严格超 zero_ref_days（「超 N 天」= age > N，恰满不计——
        # 清理候选宁保守）且 usage 表缺行（从未被检索引用）或 search_hits=0（全零）→ 计数；
        # v1 只报告不动数据（模块头 ④）。chunk_id 全局唯一（usage 行 uk 含 chunk_id）→
        # LEFT JOIN 不翻倍。表缺失（迁移未应用）= 观测缺失，置 -1 可辨且不拖垮例程
        # （同 vector_column_ready 容错先例）。
        zero_cutoff = now - timedelta(days=zero_ref_days)
        try:
            zero_ref_candidates = int(
                (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM document_chunks c"
                            " JOIN documents d ON d.id = c.document_id"
                            " LEFT JOIN kb_usage_counters u ON u.chunk_id = c.id"
                            " WHERE c.valid_to IS NULL AND d.valid_to IS NULL"
                            " AND c.created_at < :zero_cutoff"
                            " AND (u.chunk_id IS NULL OR u.search_hits = 0)"
                        ),
                        {"zero_cutoff": zero_cutoff},
                    )
                ).scalar_one()
            )
        except Exception:  # noqa: BLE001 —— 统计面容错：不掩盖 ①②③ 已产出结果
            logger.warning("零引用扫描跳过: kb_usage_counters 不可用（迁移未应用？）", exc_info=True)
            zero_ref_candidates = -1

        stats = {
            "candidates_scanned": candidates_scanned,
            "remind_groups": len(reminded),
            "remind_facts": remind_facts,
            "archive_eligible_seen": len(eligible),  # 截断时封顶 max_actions+1（溢出即预算位真值）
            "archived": len(archived),
            "actions_used": len(archived),
            "issues_missing_document": sum(1 for i in issues if i.kind == "missing_document"),
            "issues_missing_chunk": sum(1 for i in issues if i.kind == "missing_chunk"),
            "zero_ref_candidates": zero_ref_candidates,
        }
        return MaintenanceReport(
            run_id=run_id,
            now=now,
            reminded=reminded,
            archived=archived,
            issues=issues,
            stats=stats,
            budget_triggered=budget_triggered,
        )


async def _existing_refs(session: AsyncSession, column: Any, refs: set[str]) -> set[str]:
    """引用存在性批量回查：可解析 UUID 分批 IN 查询（小写归一比对）；解析失败=悬空不回查。"""
    found: set[str] = set()
    parsed: list[uuid.UUID] = []
    for ref in refs:
        try:
            parsed.append(uuid.UUID(ref))
        except (TypeError, ValueError):
            continue
    for i in range(0, len(parsed), _IN_BATCH):
        rows = await session.execute(select(column).where(column.in_(parsed[i : i + _IN_BATCH])))
        found.update(str(r[0]).lower() for r in rows)
    return found
