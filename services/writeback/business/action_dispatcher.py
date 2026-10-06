"""action_dispatcher（模块 12 一等模块：行动实例化 + 回写契约 + 对账补偿）。

权威设计：docs/MCP/业务回写设计（回写契约权威）+ docs/api/03 §7（action.invoke 特殊契约）。
外部调用一律事务外（03 篇 §6.1 两段式：先短事务落意图 → 调外部 → 再短事务落结果）；
幂等键 PG 唯一约束（uk_writeback_idem）为并发同键投递的硬兜底，重复请求返回原结果（§2.1）；
超时即 unknown 不盲目重试（§2.5，重复创建工单是回写最典型事故源）。

分层：本模块不 import 任何 L7 内部模块——连接器经 ``BizSystemAdapter`` 协议注入
（依赖倒置，锚点 §3.4 例外三；协议落点见 adapters/base.py 说明）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from services.platform.errors import ErrorCode
from services.rsi.gap import GapEvent, GapKind  # ORSI 缺口轨值对象（B9 G0；仅值依赖，见 gap_sink 说明）
from services.writeback.adapters.base import (
    AdapterError,
    BizStatusResult,
    CompensationRequest,
    ConnectorBinding,
    ConnectorRegistry,
    WritebackRequest,
)
from services.writeback.business.policy import WritebackPolicy
from services.writeback.data.repo_impl.writeback_repo import PgWritebackLedgerRepository
from services.writeback.domain.model import (
    DuplicateIdempotencyKeyError,
    LedgerStatus,
    WritebackAction,
    WritebackError,
    WritebackLedger,
)

logger = logging.getLogger("services.writeback.dispatcher")

# 未找到的 404 语义（api/03 §3.9/§2 writeback.status「未找到 404」；api/01 §4.3 404* 同族——
# 跨租户一律「不存在」不泄露存在性）。404 非 02 §7 登记码：与 REST 层 GatewayError(404) 同族
# （HTTP 状态码对齐语义，api/03 §2 显式登记），非新编业务码。
_NOT_FOUND = 404

# admin 台账面（api/01 §5.8 ★ 两端点；业务回写设计 §3.3 人工处置三动作 / §8 查询面）
_DISPOSE_ACTIONS: frozenset[str] = frozenset({"redispatch", "mark_compensated", "close"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_uuid(value: str | uuid.UUID, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError as exc:
        raise WritebackError(ErrorCode.PARAM_INVALID, f"{field} 非法 UUID: {value}") from exc


def project_entry(entry: WritebackLedger) -> dict[str, Any]:
    """台账行 → api/03 §3.7 输出契约（附加 attempts/needs_human 供状态查询面）。"""
    return {
        "ledger_id": str(entry.id),
        "idempotency_key": entry.idempotency_key,
        "status": entry.status.value,
        "receipt": dict(entry.receipt) if entry.receipt else None,
        "action_instance_id": str(entry.action_instance_id),
        "attempts": entry.attempts,
        "needs_human": entry.needs_human,
    }


def project_status(entry: WritebackLedger) -> dict[str, Any]:
    """台账行 → api/03 §3.9 writeback.status 输出契约（状态机全字段 + updated_at）。"""
    return {
        **project_entry(entry),
        "last_error": entry.last_error,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }


def _audit_note(note: str | None, actor_id: str | None) -> str | None:
    """人工处置审计注记：处置理由 + 处置人，追加式留痕（§3.3 全部留审计）。"""
    parts = [p for p in (note, f"by={actor_id}" if actor_id else None) if p]
    return "；".join(parts) or None


class ActionDispatcher:
    """行动实例化与回写负责到底（幂等/重试/对账/补偿/人工干预位）。

    装配双形态（memory provider 同款）：
    - 运行期：``session_factory``（PG 台账仓储短事务即用即弃，组合根绑定）；
    - 直注（测试）：``ledger_repo`` 单实例（进程内 Fake 仓储）。

    可选 ``gap_sink``（B9 G0，ORSI 缺口轨信号出口，09 §13.4）：行动类未绑定连接器
    （resolve-miss → ``unbound_action`` 缺口事件）时回调 ``GapEvent`` 单参函数——默认 None
    零侵入（不上报，既有错误语义不变）；只 emit 不改任何投递/状态机行为，本模块对
    rsi 仅此值对象依赖（依赖倒置：rsi 逻辑不经此边进入 writeback）。
    """

    def __init__(
        self,
        *,
        connectors: ConnectorRegistry,
        policy: WritebackPolicy | None = None,
        session_factory: Any = None,
        ledger_repo: Any = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
        gap_sink: Callable[[GapEvent], None] | None = None,
    ) -> None:
        self._connectors = connectors
        self._policy = policy or WritebackPolicy()
        self._session_factory = session_factory
        self._ledger_repo = ledger_repo
        self._sleeper = sleeper or asyncio.sleep
        self._now = clock or _utcnow
        self._gap_sink = gap_sink

    # ---------------------------------------------------------------- 装配

    @asynccontextmanager
    async def _ledger(self, tenant_id: uuid.UUID, *, commit: bool = False) -> AsyncIterator[Any]:
        if self._session_factory is not None:
            async with self._session_factory() as db:
                yield PgWritebackLedgerRepository(db, tenant_id)
                if commit:
                    await db.commit()
        elif self._ledger_repo is not None:
            yield self._ledger_repo
        else:
            raise WritebackError(ErrorCode.INTERNAL_ERROR, "台账仓储未装配（组合根未注册）")

    # ---------------------------------------------------------------- action.invoke 执行面

    async def invoke_action(
        self,
        *,
        tenant_id: uuid.UUID,
        action_iri: str,
        params: dict[str, Any],
        confirm_token: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """行动实例化 + 幂等投递（api/03 §3.7）：受理即凭证，重复请求返回原结果。"""
        if not action_iri:
            raise WritebackError(ErrorCode.PARAM_INVALID, "action_iri 为必填（须为本体行动类 IRI）")
        if not isinstance(params, dict) or not params:
            raise WritebackError(ErrorCode.PARAM_INVALID, "params 为必填（行动类参数对象）")
        binding = self._connectors.resolve(action_iri)
        if binding is None:
            self._emit_unbound_action(tenant_id=tenant_id, action_iri=action_iri, trace_id=trace_id)
            raise WritebackError(
                ErrorCode.MCP_TARGET_UNAVAILABLE, f"行动类未绑定连接器: {action_iri}（连接器注册随组合根）"
            )
        if binding.meta.risk_level == "high" and not confirm_token:
            raise WritebackError(
                ErrorCode.PARAM_INVALID,
                f"高风险行动类（risk_level=high）必须携带 confirm_token（人工二次确认，api/03 §7）: {action_iri}",
            )

        # ---- 行动实例化（OB2 行动类实例；重试回传同一 action_instance_id → 同幂等键）----
        retry_instance_raw = params.get("action_instance_id")
        action = WritebackAction.instantiate(
            tenant_id=tenant_id,
            action_iri=action_iri,
            params=params,
            risk_level=binding.meta.risk_level,
            connector_id=binding.meta.connector_id,
            action_instance_id=uuid.UUID(str(retry_instance_raw)) if retry_instance_raw else None,
        )
        payload = {
            "action_iri": action_iri,
            "params": params,
            "risk_level": binding.meta.risk_level,
            "trace_id": trace_id,
        }
        entry = WritebackLedger.create_pending(
            tenant_id=tenant_id, action=action, request_payload=payload, now=self._now()
        )
        async with self._ledger(tenant_id, commit=True) as repo:
            try:
                await repo.add(entry)  # §2.1 键在首次投递前先落台账（UK 硬兜底）
            except DuplicateIdempotencyKeyError as dup:
                logger.info(
                    "writeback idempotent replay: key=%s ledger=%s status=%s",
                    dup.existing.idempotency_key,
                    dup.existing.id,
                    dup.existing.status.value,
                )
                return project_entry(dup.existing)  # 同键已受理 → 返回首次受理结果

        # ---- 外部调用（事务外；两段式的第二段）----
        await self._dispatch(tenant_id, entry, binding)
        return project_entry(entry)

    async def _dispatch(self, tenant_id: uuid.UUID, entry: WritebackLedger, binding: ConnectorBinding) -> None:
        """重试循环（§3.1）：可重试码指数退避，业务性失败直接 failed，超时转 unknown 核实。"""
        action_iri = str(entry.request_payload.get("action_iri") or "")
        max_attempts = max(1, self._policy.max_attempts)
        for attempt in range(1, max_attempts + 1):
            entry.bump_attempt(self._now())
            await self._save(tenant_id, entry)
            request = WritebackRequest(
                idempotency_key=entry.idempotency_key,
                tenant_id=tenant_id,
                action_instance_id=entry.action_instance_id,
                action_iri=action_iri,
                params=dict(entry.request_payload.get("params") or {}),
                trace_id=entry.request_payload.get("trace_id"),
                attempt=attempt,
            )
            try:
                receipt = await asyncio.wait_for(
                    binding.adapter.execute(request), timeout=self._policy.execute_timeout_seconds
                )
            except TimeoutError:
                # §2.5：受理未知态——禁止盲目重试（重复创建工单=回写最典型事故源）
                entry.mark_unknown(self._now())
                await self._save(tenant_id, entry, commit=True)
                await self._resolve_unknown(tenant_id, entry, binding)
                return
            except AdapterError as exc:
                retryable = exc.code in self._policy.retryable_codes
                if retryable and attempt < max_attempts:
                    logger.info(
                        "writeback retryable failure: ledger=%s attempt=%s code=%s", entry.id, attempt, exc.code
                    )
                    await self._sleeper(self._policy.backoff_seconds(attempt))
                    continue
                entry.mark_failed(f"{exc.code}: {exc.message}", self._now())
                await self._save(tenant_id, entry, commit=True)
                return
            if not receipt.accepted:  # 无凭证的"成功"视为 unknown（§2.3 红线）
                entry.mark_unknown(self._now())
                await self._save(tenant_id, entry, commit=True)
                await self._resolve_unknown(tenant_id, entry, binding)
                return
            entry.mark_accepted(receipt.to_dict(), self._now())
            await self._save(tenant_id, entry, commit=True)
            logger.info(
                "writeback accepted: ledger=%s receipt_no=%s attempts=%s",
                entry.id,
                receipt.receipt_no,
                entry.attempts,
            )
            return
        entry.mark_failed(
            f"{ErrorCode.RETRY_BUDGET_EXHAUSTED}: 重试预算耗尽（max_attempts={max_attempts}）", self._now()
        )
        await self._save(tenant_id, entry, commit=True)

    async def _resolve_unknown(self, tenant_id: uuid.UUID, entry: WritebackLedger, binding: ConnectorBinding) -> None:
        """unknown 处置（§2.5）：先 query_status 按键核实，能判定落终态，否则挂对账/人工位。"""
        result = await self._query_status_safe(binding, entry.idempotency_key)
        await self._settle_from_query(tenant_id, entry, binding, result)

    # ---------------------------------------------------------------- 状态回执回流 / writeback.status 面

    async def status(
        self,
        *,
        tenant_id: uuid.UUID,
        ledger_id: str | uuid.UUID | None = None,
        idempotency_key: str | None = None,
        action_instance_id: str | uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """writeback.status（api/03 §3.9/§2）：三键任一定位台账行（tenant 过滤），返回状态机全字段。

        - 定位优先级 ledger_id > idempotency_key > action_instance_id（后者经幂等键
          ``{tenant_id}:{action_instance_id}`` 前缀重组定位，§2.1 键式唯一）；
        - 未找到 404 语义（api/03 §2 登记口径；跨租户同口径「不存在」，不泄露存在性）；
        - 非终态主动核实（accepted/unknown 先 query_status 再定性，§2.4/§2.5——
          「超时即 unknown 不盲目重试」红线同源），经状态机前向迁移落账。
        """
        async with self._ledger(tenant_id) as repo:
            if ledger_id:
                entry = await repo.get(_coerce_uuid(ledger_id, "ledger_id"))
            elif idempotency_key:
                entry = await repo.get_by_idempotency_key(idempotency_key)
            elif action_instance_id:
                instance = _coerce_uuid(action_instance_id, "action_instance_id")
                entry = await repo.get_by_idempotency_key(f"{tenant_id}:{instance}")
            else:
                raise WritebackError(
                    ErrorCode.PARAM_INVALID, "须提供 ledger_id / idempotency_key / action_instance_id 之一"
                )
        if entry is None:
            raise WritebackError(_NOT_FOUND, "台账行不存在（按所给键未命中或跨租户）")
        if entry.is_terminal:
            return project_status(entry)  # 终态幂等原样返回（只前进不回退）
        binding = self._require_binding(entry.connector_id)
        result = await self._query_status_safe(binding, entry.idempotency_key)
        await self._settle_from_query(tenant_id, entry, binding, result)
        return project_status(entry)

    async def refresh_status(self, *, tenant_id: uuid.UUID, ledger_id: uuid.UUID) -> dict[str, Any]:
        """状态回执回流（§2.4）：writeback.status 的 ledger_id 单键形态（api/03 §3.9 同面）。"""
        return await self.status(tenant_id=tenant_id, ledger_id=ledger_id)

    async def _settle_from_query(
        self,
        tenant_id: uuid.UUID,
        entry: WritebackLedger,
        binding: ConnectorBinding,
        result: BizStatusResult | None,
    ) -> None:
        """query_status 结果 → 台账定性（能判定落终态；超对账时限不可判定进人工队列 §3.3）。"""
        if result is not None and result.finished and result.success is True:
            receipt = self._receipt_from_status(result)
            if entry.status == LedgerStatus.PENDING:  # pending→succeeded 非法：先受理后成功（§2.5 两步）
                entry.mark_accepted(receipt, self._now())
            entry.mark_succeeded(self._now(), receipt=receipt)
        elif result is not None and result.finished and result.success is False:
            entry.mark_failed(f"BIZ_FAILED: 业务侧终态 {result.status}", self._now())
        else:
            age = (self._now() - (entry.updated_at or entry.created_at or self._now())).total_seconds()
            if age > self._policy.recon_deadline_hours * 3600:
                entry.note_incident(
                    f"RECON_DEADLINE: unknown 超对账时限（{self._policy.recon_deadline_hours}h）不可核实",
                    self._now(),
                )
        await self._save(tenant_id, entry, commit=True)

    async def _query_status_safe(self, binding: ConnectorBinding, idempotency_key: str) -> BizStatusResult | None:
        if not binding.meta.supports_query_status:
            return None
        try:
            return await asyncio.wait_for(
                binding.adapter.query_status(idempotency_key), timeout=self._policy.execute_timeout_seconds
            )
        except (AdapterError, TimeoutError) as exc:
            logger.info("writeback query_status unavailable: key=%s err=%s", idempotency_key, exc)
            return None

    @staticmethod
    def _receipt_from_status(result: BizStatusResult) -> dict[str, Any]:
        return {
            "accepted": True,
            "receipt_no": result.receipt_no or "",
            "idempotency_key": result.raw.get("idempotency_key", ""),
            "occurred_at": str(result.raw.get("created_at") or ""),
            "raw": dict(result.raw),
        }

    # ---------------------------------------------------------------- 补偿（§3.2 Saga 式逆向）

    async def compensate(
        self, *, tenant_id: uuid.UUID, ledger_id: uuid.UUID, reason: str, trace_id: str | None = None
    ) -> dict[str, Any]:
        """业务冲正：失败/未知/（对账修正的）成功态 → compensated；补偿失败进人工队列不静默。"""
        if not reason:
            raise WritebackError(ErrorCode.PARAM_INVALID, "补偿必须携带 reason（审计留痕）")
        async with self._ledger(tenant_id) as repo:
            entry = await repo.get(ledger_id)
        if entry is None:
            raise WritebackError(ErrorCode.PARAM_INVALID, f"台账行不存在: {ledger_id}")
        if entry.status == LedgerStatus.COMPENSATED:
            return project_entry(entry)  # 补偿幂等：已冲正原样返回
        if entry.status == LedgerStatus.PENDING:
            raise WritebackError(ErrorCode.VERSION_CONFLICT, "pending 态不可补偿（业务尚未受理，§2.5 状态图无此迁移）")
        binding = self._require_binding(entry.connector_id)
        if not binding.meta.supports_compensate:
            raise WritebackError(ErrorCode.PARAM_INVALID, f"连接器不支持补偿: {binding.meta.name}")
        request = CompensationRequest(
            idempotency_key=f"{entry.idempotency_key}:compensate",
            tenant_id=tenant_id,
            action_instance_id=entry.action_instance_id,
            action_iri=str(entry.request_payload.get("action_iri") or ""),
            original_receipt=dict(entry.receipt) if entry.receipt else {},
            reason=reason,
            trace_id=trace_id,
        )
        try:
            receipt = await asyncio.wait_for(
                binding.adapter.compensate(request), timeout=self._policy.execute_timeout_seconds
            )
        except TimeoutError:
            entry.note_incident("COMPENSATE_TIMEOUT: 冲正超时（响应丢失，需人工核实）", self._now())
            await self._save(tenant_id, entry, commit=True)
            raise WritebackError(ErrorCode.MCP_TARGET_UNAVAILABLE, "补偿超时：已转人工干预队列") from None
        except AdapterError as exc:
            entry.note_incident(f"COMPENSATE_FAILED: {exc.code}: {exc.message}", self._now())
            await self._save(tenant_id, entry, commit=True)
            raise WritebackError(
                ErrorCode.PARAM_INVALID, f"补偿失败（进人工干预队列）: {exc.code}: {exc.message}"
            ) from exc
        entry.mark_compensated(receipt.to_dict(), self._now())
        await self._save(tenant_id, entry, commit=True)
        logger.info("writeback compensated: ledger=%s reason=%s", entry.id, reason)
        return project_entry(entry)

    # ---------------------------------------------------------------- admin 台账面（api/01 §5.8；§3.3/§8）

    async def list_ledger(
        self,
        *,
        tenant_id: uuid.UUID,
        status: str | None = None,
        needs_human: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """台账分页查询（api/01 §5.8 GET /admin/writeback/ledger；§8 查询面）。

        薄投影复用 project_status（= project_entry + last_error/updated_at，与单条详情/
        writeback.status 同形，api/01 §6.5 示例行全字段）；租户过滤在仓储层（他租户行不可见）；
        status 非法值 3001（api/03 §3.9 状态枚举）。
        """
        status_filter: LedgerStatus | None = None
        if status is not None:
            try:
                status_filter = LedgerStatus(status)
            except ValueError as exc:
                raise WritebackError(ErrorCode.PARAM_INVALID, f"非法台账状态: {status}（§2.5 状态枚举）") from exc
        async with self._ledger(tenant_id) as repo:
            items, total = await repo.list_page(
                tenant_id, status=status_filter, needs_human=needs_human, offset=offset, limit=limit
            )
        return {"items": [project_status(e) for e in items], "total": total, "offset": offset, "limit": limit}

    async def dispose(
        self,
        *,
        tenant_id: uuid.UUID,
        ledger_id: str | uuid.UUID,
        action: str,
        note: str | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        """人工处置（api/01 §5.8 POST /admin/writeback/ledger/{id}/dispose；§3.3 三动作）。

        - redispatch：unknown/failed（含挂人工位的 pending）重开投递窗口——同幂等键新 attempt
          （业务侧按键幂等不重复创建，§2.2）；投递经 ``run_redispatch_delivery`` 后台执行；
        - mark_compensated：原单有受理凭证 → 委托 ``compensate``（§3.2 业务冲正契约，凭证据证）；
          无凭证 → 显式人工标记（note 必填；人工标记收据落 receipt 审计位，``manual=true``）；
        - close：附理由前向定性 failed 终局 + needs_human 清位（§3.3 关闭附理由）；
        - B8.1 加固：①「标记+落库」= 行锁单事务（``get_for_update`` → 状态守卫复核 → 聚合迁移
          → commit），防并发写者 lost-update（_save 全字段覆盖仅在锁持有期内安全）；
          ②返回投影 = 落库后从仓储重取最新行（以 DB 为准，不再投影内存旧快照）；
          ③redispatch 投递在事务外后台完成（202=受理即返，不阻塞 HTTP 响应）；
        - 状态迁移必经聚合方法（前向纪律）；不可处置态（终态关闭/accepted 重发等）3003→REST 409；
          留痕统一追加写 ledger.last_error（聚合唯一自由文本审计位，docstring 见聚合方法）。
        """
        if action not in _DISPOSE_ACTIONS:
            raise WritebackError(
                ErrorCode.PARAM_INVALID, f"action 须为 redispatch/mark_compensated/close 之一: {action}"
            )
        entry_id = _coerce_uuid(ledger_id, "ledger_id")
        if action == "redispatch":
            await self._dispose_redispatch_mark(tenant_id, entry_id, _audit_note(note, actor_id))
        elif action == "mark_compensated":
            await self._dispose_compensate(tenant_id, entry_id, note, actor_id)
        else:
            await self._dispose_close(tenant_id, entry_id, _audit_note(note, actor_id))
        return await self._project_fresh(tenant_id, entry_id)

    async def _project_fresh(self, tenant_id: uuid.UUID, entry_id: uuid.UUID) -> dict[str, Any]:
        """dispose 返回投影：动作落库后从仓储重取最新行（B8.1 修复②，以 DB 为准）。

        compensate 委托路径在自建会话写库，dispose 手里的内存快照已陈旧——投影内存快照会把
        已冲正行答成受理态。台账只前进不删除，重取不可能 404（防御分支保口径一致）。
        """
        async with self._ledger(tenant_id) as repo:
            entry = await repo.get(entry_id)
        if entry is None:
            raise WritebackError(_NOT_FOUND, "台账行不存在（按所给键未命中或跨租户）")
        return project_status(entry)

    @staticmethod
    def _guard_redispatch(entry: WritebackLedger) -> None:
        """重发守卫（§3.3）：终态/accepted 不可重发；pending 未挂人工位无需人工重发。"""
        if entry.is_terminal or entry.status is LedgerStatus.ACCEPTED:
            raise WritebackError(
                ErrorCode.VERSION_CONFLICT,
                f"台账状态不可重发: {entry.status.value}（终态/accepted 不可重发——accepted 处置走冲正或关闭，§3.3）",
                detail={"ledger_id": str(entry.id), "status": entry.status.value},
            )
        if entry.status is LedgerStatus.PENDING and not entry.needs_human:
            raise WritebackError(
                ErrorCode.VERSION_CONFLICT,
                "pending 态在投递窗口内（对账/重试自然续投），无需人工重发",
                detail={"ledger_id": str(entry.id)},
            )

    async def _dispose_redispatch_mark(self, tenant_id: uuid.UUID, entry_id: uuid.UUID, note: str | None) -> None:
        """重发·标记段（行锁单事务，B8.1 修复①）：守卫复核 → 重开投递窗口落库。

        投递段在事务外：``run_redispatch_delivery`` 后台执行（修复③，202 不等重试循环）。
        """
        async with self._ledger(tenant_id, commit=True) as repo:
            entry = await repo.get_for_update(tenant_id, entry_id)
            if entry is None:
                raise WritebackError(_NOT_FOUND, "台账行不存在（按所给键未命中或跨租户）")
            self._guard_redispatch(entry)  # 锁内复核：并发下状态可能已变（另一 dispose/对账先行）
            entry.mark_redispatched(self._now(), note=note)
            await repo.save_state(entry)

    async def run_redispatch_delivery(self, *, tenant_id: uuid.UUID, ledger_id: str | uuid.UUID) -> dict[str, Any]:
        """重发·投递段（后台/事务外，B8.1 修复③）：重取台账行 → 守卫仍 pending → 复用 ``_dispatch``。

        dispose(redispatch) 标记段落库（pending + needs_human=false）即返 202；本方法由路由层
        经 BackgroundTasks 后台执行（最坏重试 ~90s 不得阻塞 HTTP 响应）。守卫不满足（行已被
        并发写者推进/不在投递窗口）= 幂等空转不投递，返回当前投影（以 DB 为准防竞态覆盖）。
        """
        entry_id = _coerce_uuid(ledger_id, "ledger_id")
        async with self._ledger(tenant_id) as repo:
            entry = await repo.get(entry_id)
        if entry is None:
            raise WritebackError(_NOT_FOUND, "台账行不存在（按所给键未命中或跨租户）")
        if entry.status is not LedgerStatus.PENDING:
            logger.info("writeback redispatch skip (not pending): ledger=%s status=%s", entry.id, entry.status.value)
            return project_status(entry)
        binding = self._require_binding(entry.connector_id)
        await self._dispatch(tenant_id, entry, binding)  # 复用投递循环：同幂等键新 attempt
        return project_status(entry)

    async def _dispose_compensate(
        self, tenant_id: uuid.UUID, entry_id: uuid.UUID, note: str | None, actor_id: str | None
    ) -> None:
        """标记冲正（§3.3）：守卫在行锁内复核；冲正落库分两路——

        - 无凭证 → 行锁事务内显式人工标记（note 必填；manual 收据落 receipt 审计位）；
        - 有凭证 → 锁外委托 ``compensate``（自建会话业务冲正，§3.2）：行锁持有会与其落库
          短事务互等死锁，故锁内只做守卫复核，提交释放行锁后再委托。
        """
        delegate_reason: str | None = None
        async with self._ledger(tenant_id, commit=True) as repo:
            entry = await repo.get_for_update(tenant_id, entry_id)
            if entry is None:
                raise WritebackError(_NOT_FOUND, "台账行不存在（按所给键未命中或跨租户）")
            if entry.receipt:
                delegate_reason = _audit_note(note or "人工标记冲正（原单凭证据证）", actor_id) or "人工标记冲正"
            else:
                if not note:
                    raise WritebackError(ErrorCode.PARAM_INVALID, "无凭证的人工标记冲正必须附 note（审计留痕，§3.3）")
                receipt = {
                    "accepted": True,
                    "receipt_no": "",
                    "idempotency_key": f"{entry.idempotency_key}:compensate",
                    "occurred_at": self._now().isoformat(),
                    "manual": True,  # 显式人工标记（无业务凭证；审计依据=note+处置人，§3.3 留审计）
                    "note": note,
                    "marked_by": actor_id,
                }
                entry.mark_compensated(receipt, self._now())  # 迁移合法性由聚合闸门（pending/终态外均可）
                await repo.save_state(entry)
        if delegate_reason is not None:
            await self.compensate(tenant_id=tenant_id, ledger_id=entry_id, reason=delegate_reason)

    async def _dispose_close(self, tenant_id: uuid.UUID, entry_id: uuid.UUID, note: str | None) -> None:
        """关闭·标记段（行锁单事务，B8.1 修复①）：附理由守卫复核 → 前向定性 failed 落库。"""
        if not note:
            raise WritebackError(ErrorCode.PARAM_INVALID, "关闭必须附理由（§3.3 关闭附理由，审计留痕）")
        async with self._ledger(tenant_id, commit=True) as repo:
            entry = await repo.get_for_update(tenant_id, entry_id)
            if entry is None:
                raise WritebackError(_NOT_FOUND, "台账行不存在（按所给键未命中或跨租户）")
            if entry.is_terminal:  # 锁内复核：终态不可关闭（succeeded/compensated 已终局，§2.5）
                raise WritebackError(
                    ErrorCode.VERSION_CONFLICT,
                    f"终态不可关闭: {entry.status.value}（succeeded/compensated 已终局，§2.5）",
                    detail={"ledger_id": str(entry.id), "status": entry.status.value},
                )
            entry.mark_closed(note, self._now())
            await repo.save_state(entry)

    # ---------------------------------------------------------------- 对账（§4）

    async def reconcile(
        self,
        *,
        tenant_id: uuid.UUID,
        limit: int = 100,
        include_terminal: bool = False,
    ) -> dict[str, Any]:
        """对账查询面（§4.1/§4.2）：台账 vs query_status，差异分类并自动处置，产出报告。

        差异处置：可判定 → 补记终态；「平台成功业务失败」→ 自动冲正（§3.2 触发条件）；
        不可判定/连接器不支持补偿 → needs_human 人工干预队列。报告写审计留痕位（08 §3）。
        """
        statuses = [LedgerStatus.PENDING, LedgerStatus.ACCEPTED, LedgerStatus.UNKNOWN]
        if include_terminal:
            # §4.1 比对范围含终态（succeeded/failed）——「平台失败业务成功」差异仅终态扫描可发现
            statuses += [LedgerStatus.SUCCEEDED, LedgerStatus.FAILED]
        async with self._ledger(tenant_id) as repo:
            entries = await repo.list_by_statuses(statuses, limit=limit)
        report: dict[str, Any] = {"checked": 0, "diffs": [], "fixed": [], "compensated": [], "needs_human": []}
        for entry in entries:
            report["checked"] += 1
            binding = self._connectors.by_connector_id(entry.connector_id)
            if binding is None or not binding.meta.supports_query_status:
                continue
            result = await self._query_status_safe(binding, entry.idempotency_key)
            unresolvable = result is None or not result.finished or result.success is None
            age = (self._now() - (entry.updated_at or entry.created_at or self._now())).total_seconds()
            if unresolvable and not entry.is_terminal and age > self._policy.recon_deadline_hours * 3600:
                # 双 unknown/仍在途且超对账时限：人工干预队列（§4.2 HM 分支）
                entry.note_incident(
                    f"RECON_DEADLINE: 对账超时（{self._policy.recon_deadline_hours}h）不可核实", self._now()
                )
                await self._save(tenant_id, entry, commit=True)
                report["diffs"].append(
                    {
                        "ledger_id": str(entry.id),
                        "diff_type": "RECON_DEADLINE_EXCEEDED",
                        "biz_status": result.status if result else None,
                    }
                )
                report["needs_human"].append({"ledger_id": str(entry.id), "reason": "RECON_DEADLINE_EXCEEDED"})
                continue
            diff = self._classify(entry, result)
            if diff is None:
                continue
            report["diffs"].append(
                {"ledger_id": str(entry.id), "diff_type": diff, "biz_status": result.status if result else None}
            )
            if diff == "PLATFORM_SUCCESS_BIZ_FAILED":
                if binding.meta.supports_compensate:
                    try:
                        await self.compensate(
                            tenant_id=tenant_id, ledger_id=entry.id, reason=f"RECON: {diff}", trace_id=None
                        )
                        report["compensated"].append(str(entry.id))
                    except WritebackError as exc:
                        report["needs_human"].append({"ledger_id": str(entry.id), "reason": exc.message})
                else:
                    entry.note_incident(f"RECON: {diff}（连接器不支持补偿）", self._now())
                    await self._save(tenant_id, entry, commit=True)
                    report["needs_human"].append({"ledger_id": str(entry.id), "reason": diff})
                continue
            # 其余差异：按 query 定性补记终态（不直接改写历史行——经状态机前向迁移）；
            # 非法迁移（如 failed→succeeded）= 状态机不可达终态 → 人工干预（§4.2 HM 分支）
            try:
                await self._settle_from_query(tenant_id, entry, binding, result)
            except WritebackError as exc:
                entry.note_incident(f"RECON_UNSETTLEABLE: {exc.message}", self._now())
                await self._save(tenant_id, entry, commit=True)
                report["needs_human"].append({"ledger_id": str(entry.id), "reason": exc.message})
                continue
            report["fixed"].append(str(entry.id))
        logger.info("writeback recon report: tenant=%s report=%s", tenant_id, report)
        return report

    @staticmethod
    def _classify(entry: WritebackLedger, result: BizStatusResult | None) -> str | None:
        """差异分类（§4.2）：平台态 vs 业务终态不一致判定；一致/不可判定返回 None。"""
        if result is None or not result.finished or result.success is None:
            return None  # 业务侧仍在途/不可判定：终态定性交给后续轮次（unknown 超期走 note_incident）
        if result.success is True:
            if entry.status in (LedgerStatus.PENDING, LedgerStatus.ACCEPTED, LedgerStatus.UNKNOWN):
                return "PLATFORM_NOT_TERMINAL_BIZ_DONE"  # 补记 succeeded（经状态机前向迁移）
            if entry.status == LedgerStatus.FAILED:
                return "PLATFORM_FAILED_BIZ_DONE"  # failed→succeeded 非法迁移（§2.5）→ 人工裁决
            return None  # succeeded 与业务一致
        if entry.status in (LedgerStatus.PENDING, LedgerStatus.ACCEPTED, LedgerStatus.SUCCEEDED):
            return "PLATFORM_SUCCESS_BIZ_FAILED"  # 冲正触发（§3.2 补偿触发条件）
        if entry.status == LedgerStatus.UNKNOWN:
            return "PLATFORM_UNKNOWN_BIZ_FAILED"  # 核实为失败 → 补记 failed（unknown→failed 合法）
        return None  # failed/compensated 与业务一致

    # ---------------------------------------------------------------- 内部

    def _require_binding(self, connector_id: uuid.UUID) -> ConnectorBinding:
        binding = self._connectors.by_connector_id(connector_id)
        if binding is None:
            raise WritebackError(ErrorCode.MCP_TARGET_UNAVAILABLE, f"连接器未注册: {connector_id}（组合根装配缺位）")
        return binding

    def _emit_unbound_action(self, *, tenant_id: uuid.UUID, action_iri: str, trace_id: str | None) -> None:
        """resolve-miss → ORSI 缺口轨 ``unbound_action`` 事件（09 §13.4；gap_sink 未装配则零操作）。

        emit 点选在 ``invoke_action`` 的 ``connectors.resolve`` 未命中处（行动类存在但无实现
        绑定的真实发生位，§13.2 O1 触发信号）；``_require_binding`` 是既有台账行按 connector_id
        的重查（无 action_iri 上下文，语义属装配缺位），不在其上发缺口事件。只上报不改变
        既有错误语义（仍抛 MCP_TARGET_UNAVAILABLE）。
        """
        if self._gap_sink is None:
            return
        self._gap_sink(
            GapEvent(
                tenant_id=tenant_id,
                kind=GapKind.UNBOUND_ACTION,
                action_iri=action_iri,
                occurred_at=self._now(),
                source="writeback.dispatcher.resolve_miss",
                trace_ids=(trace_id,) if trace_id else (),
            )
        )

    async def _save(self, tenant_id: uuid.UUID, entry: WritebackLedger, *, commit: bool = False) -> None:
        """台账状态持久化（独立短事务；外部调用间隙崩溃=台账留在上一稳定态，重启可续）。"""
        async with self._ledger(tenant_id, commit=commit) as repo:
            await repo.save_state(entry)
