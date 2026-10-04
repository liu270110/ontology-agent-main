"""平台能力 provider（api/03 §3 逐工具实现面 + §3.9 writeback.status；07 篇 §1 依赖倒置的 L3/L5 侧适配器）。

接线纪律（跨模块 import 探测结论，见模块报告）：
- ontology 工具 → services.ontology.core / business（公开面，传递链干净）；
- memory 工具 → services.memory.business / memory.domain.repo 协议（仓储以协议注入，
  组合根绑定；mcp 禁入任何模块 data/ 内部）；
- knowledge.search → 检索协作对象以本地 Protocol 注入（本文件仍零 kb import；kb 公开检索
  服务的直连装配收口在共享工厂 services/mcp/bootstrap.py——P1-1 收口后豁免边已合入 pyproject）；
- action.invoke → ActionCapabilityProvider：执行面以 ActionDispatcherPort 协议注入
  （实现=writeback.business.ActionDispatcher，组合根绑定）；
- writeback.status → WritebackStatusCapabilityProvider：查询面以 WritebackStatusPort 协议注入
  （同一 dispatcher 承载；三键定位/404 语义/主动核实全在 dispatcher，api/03 §3.9 权威）——
  mcp 零 writeback 内部分层 import，幂等键/受理凭证/状态机/补偿全在 dispatcher（业务回写设计权威）。

推理分级红线（宪法 2）：ontology.reason 的 semantic/entailment 路由（LLM 类）在模型端口
未绑定或校验未通过时 verdict 只能是 ``candidate``，validation 回执如实标注。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final, Protocol, runtime_checkable

from services.memory.business.context import merge_l2_hits
from services.memory.domain.model.l1 import MemoryBlock
from services.memory.domain.model.l2_fact import FactCategory, L2Fact, fact_fingerprint
from services.memory.domain.repo.fact_repo import L1MemoryStore, L2FactRepository
from services.ontology.business.hierarchy_service import get_class_hierarchy
from services.ontology.business.ontology_gate import run_changeset_gate
from services.ontology.core import load_turtle
from services.platform.ports.capability_provider import (
    KERNEL_LOOP_VERSION,
    CallContext,
    CapabilityDescriptor,
    CapabilityError,
    CapabilityResult,
)

# ---------------------------------------------------------------- 注入协议（禁 import 各模块 data/）


@runtime_checkable
class OntologyArtifactLoader(Protocol):
    """本体制品装载协议：按 (tenant, ontology_id, version|None=当前发布版) 取 Turtle 制品文本。

    实现方 = 组合根绑定 ontology 仓储制品面（M4 组合收口）；未绑定则相关工具结构化降级。
    """

    async def load(
        self, tenant_id: uuid.UUID, ontology_id: uuid.UUID, version: str | None = None
    ) -> str: ...  # pragma: no cover — Protocol 方法无实现


@runtime_checkable
class KnowledgeSearchFn(Protocol):
    """knowledge.search 协作对象协议（形状 = kb 公开检索服务 search 的关键字签名与返回属性）。"""

    async def search(
        self,
        *,
        tenant_id: uuid.UUID,
        query: str,
        kb_id: uuid.UUID | None = None,
        top_k: int = 8,
        mode: str = "local",
        entity_type_filter: Sequence[str] | None = None,
        with_evidence: bool = True,
        acl_tags: Sequence[str] | None = None,
    ) -> (
        Any
    ): ...  # pragma: no cover — Protocol 方法无实现（返回 duck-typed：degraded/citations/graph_paths/latency_ms）


_SPARQL_WRITE_SYNTAX: Final[re.Pattern[str]] = re.compile(
    r"\b(INSERT|DELETE|UPDATE|DROP|CLEAR|CREATE|LOAD|COPY|MOVE|ADD|CONSTRUCT\s*\{?\s*.*\bDELETE)\b",
    re.IGNORECASE | re.DOTALL,
)


def _reject_write_sparql(sparql: str) -> None:
    """只读 SPARQL 门禁（api/03 §3.4：含 UPDATE/DELETE 语法即 3001 拒绝）。"""
    from services.platform.errors import ErrorCode

    if _SPARQL_WRITE_SYNTAX.search(sparql):
        raise CapabilityError(int(ErrorCode.PARAM_INVALID), "只读 SPARQL：含写语法（INSERT/DELETE/UPDATE 等）即拒绝")


def _rdflib_rows(graph: Any, sparql: str) -> dict[str, Any]:
    """SPARQL SELECT → {columns, rows}（URIRef/Literal 统一字符串化；api/03 §3.4 输出契约）。"""
    result = graph.query(sparql)
    columns = [str(v) for v in result.vars]
    rows = [[None if v is None else str(v) for v in row] for row in result]
    return {"columns": columns, "rows": rows}


# ---------------------------------------------------------------- ontology


class OntologyCapabilityProvider:
    """ontology.validate / ontology.reason / ontology.query（api/03 §3.2~§3.4）。"""

    def __init__(
        self,
        *,
        artifact_loader: OntologyArtifactLoader | None = None,
        session_factory: Any = None,
    ) -> None:
        self.provider_version = "0.1.0"
        self.supported_loop_versions = [KERNEL_LOOP_VERSION]
        self._loader = artifact_loader
        self._session_factory = session_factory

    def namespace(self) -> str:
        return "ontology"

    def capabilities(self) -> list[CapabilityDescriptor]:
        return [
            CapabilityDescriptor(
                name="ontology.validate",
                description="SHACL 校验：data graph × 当前发布版 shapes 制品（门禁语义：失败即违规清单）",
                required_scopes=("ontology:read",),
                annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
                semantic={"x-ontology": {"shape_version": "current", "candidate_source": "extraction|manual|agent"}},
            ),
            CapabilityDescriptor(
                name="ontology.reason",
                description=(
                    "本体推理：consistency（规则+SHACL）/ classification（层次闭包）/ "
                    "entailment|semantic（LLM+gate，未过校验 verdict=candidate）"
                ),
                required_scopes=("ontology:read",),
                annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
                semantic={"x-ontology": {"reasoning_route": "rule|owl|llm+gate"}},
            ),
            CapabilityDescriptor(
                name="ontology.query",
                description="只读 SPARQL 查询（当前发布版 TBox）；含写语法即 3001 拒绝",
                required_scopes=("ontology:read",),
                annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
                semantic={"x-ontology": {"readonly_sparql": True, "write_syntax_rejected": 3001}},
            ),
        ]

    async def invoke(self, name: str, params: dict[str, Any], ctx: CallContext) -> CapabilityResult:
        local = name.split(".", 1)[1]
        if local == "validate":
            return CapabilityResult.success(await self._validate(params, ctx))
        if local == "reason":
            return CapabilityResult.success(await self._reason(params, ctx))
        if local == "query":
            return CapabilityResult.success(await self._query(params, ctx))
        raise CapabilityError(3001, f"能力未实现: {name}")

    # ---- validate（api/03 §3.2）----

    async def _validate(self, params: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        if self._loader is None:
            raise CapabilityError(5004, "本体制品装载器未装配（组合根未注册），validate 降级不可用")
        ontology_id = _parse_uuid(params.get("ontology_id"), "ontology_id")
        data_text = params.get("graph")
        if not data_text:
            raise CapabilityError(3001, "graph（待校验 RDF 图，Turtle）为必填；data_graph_uri 解引用随 M5")
        shapes_text = await self._loader.load(ctx.tenant_id, ontology_id, params.get("shape_version") or None)
        from services.ontology.core import validate as shacl_validate

        try:
            data_graph = await asyncio.to_thread(load_turtle, str(data_text))
            shapes_graph = await asyncio.to_thread(load_turtle, shapes_text)
        except ValueError as exc:
            raise CapabilityError(3001, str(exc)) from exc
        report = await asyncio.to_thread(shacl_validate, data_graph, shapes_graph)
        return {
            "conforms": report.conforms,
            "stats": {"triples": len(data_graph), "elapsed_ms": report.elapsed_ms},
            "results": [v.model_dump() for v in report.results],
        }

    # ---- reason（api/03 §3.3）----

    async def _reason(self, params: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        reason_type = params.get("type")
        if reason_type == "consistency":
            return await self._reason_consistency(params, ctx)
        if reason_type == "classification":
            return await self._reason_classification(params, ctx)
        if reason_type in ("entailment", "semantic"):
            return self._reason_candidate(reason_type)
        raise CapabilityError(3001, f"不支持的推理类型: {reason_type}")

    async def _reason_consistency(self, params: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        """consistency = 服务端硬门禁同款实跑（lint 三路由 + SHACL 自校验，规则路由非 LLM）。"""
        if self._loader is None:
            raise CapabilityError(5004, "本体制品装载器未装配（组合根未注册），consistency 降级不可用")
        ontology_id = _parse_uuid(params.get("ontology_id"), "ontology_id")
        turtle = await self._loader.load(ctx.tenant_id, ontology_id, None)
        report = await run_changeset_gate(turtle)
        return {
            "verdict": "pass" if report.conforms else "fail",
            "answer": "TBox 一致性校验通过（lint+SHACL 自校验）" if report.conforms else "门禁未通过，存在违规",
            "evidence": [v.code or v.stage for v in report.violations[:20]],
            "validation": {"owl": False, "shacl": report.shacl_conforms, "rules": report.lint_ok},
        }

    async def _reason_classification(self, params: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        """classification = 类层次闭包判定（读模型层次服务，确定性规则路由）。"""
        if self._session_factory is None:
            raise CapabilityError(5004, "会话工厂未装配，classification 降级不可用")
        ontology_id = _parse_uuid(params.get("ontology_id"), "ontology_id")
        payload = params.get("input") or {}
        class_iri = str(payload.get("class_iri") or "")
        if not class_iri:
            raise CapabilityError(3001, "classification 需 input.class_iri")
        async with self._session_factory() as db:
            rows = await get_class_hierarchy(db, tenant_id=ctx.tenant_id, ontology_id=ontology_id)
        if not any(class_iri == row.iri or class_iri in row.subclass_of for row in rows):
            return {
                "verdict": "fail",
                "answer": f"类 {class_iri} 不在当前发布版类层次中",
                "evidence": [],
                "validation": {"owl": False, "shacl": False, "rules": True},
            }
        chain = next((row.subclass_of for row in rows if row.iri == class_iri), [])
        return {
            "verdict": "pass",
            "answer": f"{class_iri} 的超类闭包: {chain}",
            "evidence": chain,
            "validation": {"owl": False, "shacl": False, "rules": True},
        }

    def _reason_candidate(self, reason_type: str) -> dict[str, Any]:
        """LLM 路由（entailment/semantic）：M4.1 未接推理引擎——按红线只出 candidate，不出成品结论。"""
        return {
            "verdict": "candidate",
            "answer": (
                f"{reason_type} 推理走 LLM+gate 路由，模型端口未装配（M4.1 欠账）；候选结论须经规则校验后才可成品"
            ),
            "evidence": [],
            "validation": {"owl": False, "shacl": False, "rules": False},
        }

    # ---- query（api/03 §3.4）----

    async def _query(self, params: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        if self._loader is None:
            raise CapabilityError(5004, "本体制品装载器未装配（组合根未注册），query 降级不可用")
        ontology_id = _parse_uuid(params.get("ontology_id"), "ontology_id")
        sparql = str(params.get("sparql") or "")
        if not sparql:
            raise CapabilityError(3001, "sparql 为必填")
        _reject_write_sparql(sparql)
        timeout_ms = int(params.get("timeout_ms") or 5000)
        if timeout_ms <= 0 or timeout_ms > 30000:
            raise CapabilityError(3001, "timeout_ms 须在 (0, 30000]")
        turtle = await self._loader.load(ctx.tenant_id, ontology_id, None)
        try:
            graph = await asyncio.to_thread(load_turtle, turtle)
        except ValueError as exc:
            raise CapabilityError(3001, str(exc)) from exc
        started = time.perf_counter()
        try:
            rows = await asyncio.wait_for(asyncio.to_thread(_rdflib_rows, graph, sparql), timeout_ms / 1000)
        except TimeoutError:
            raise CapabilityError(3001, f"SPARQL 查询超时（>{timeout_ms}ms）") from None
        rows["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return rows


# ---------------------------------------------------------------- memory


class MemoryCapabilityProvider:
    """memory.read / memory.write / memory.invalidate（api/03 §3.5/§3.6/§3.8）。

    层级命名映射（MCP 篇 §2 裁决，唯一映射点）：session↔L1 / user↔L2 / org↔L3（M5 恒空集）/
    knowledge↔L4（走 knowledge.search，本工具恒空集）。仓储装配双形态：
    - 运行期（推荐）：``session_factory + l2_repo_builder``（builder=memory.business.runtime.build_l2_repo，
      公开装配面；短会话即用即弃，写路径成功提交）；
    - 直注（测试/固定租户）：``l2_repo`` 单实例。
    """

    def __init__(
        self,
        *,
        l1_store: L1MemoryStore | None = None,
        l2_repo: L2FactRepository | None = None,
        session_factory: Any = None,
        l2_repo_builder: Callable[[Any, uuid.UUID], L2FactRepository] | None = None,
        rrf_k: int = 60,
        half_life_days: float = 30,
    ) -> None:
        self.provider_version = "0.1.0"
        self.supported_loop_versions = [KERNEL_LOOP_VERSION]
        self._l1 = l1_store
        self._l2 = l2_repo
        self._session_factory = session_factory
        self._l2_repo_builder = l2_repo_builder
        self._rrf_k = rrf_k
        self._half_life_days = half_life_days

    @asynccontextmanager
    async def _l2_for(self, ctx: CallContext, *, commit: bool = False) -> AsyncIterator[L2FactRepository]:
        # 按租户取 L2 仓储：短会话优先（运行期装配）；未装配抛 5004 结构化降级
        if self._session_factory is not None and self._l2_repo_builder is not None:
            async with self._session_factory() as db:
                yield self._l2_repo_builder(db, ctx.tenant_id)
                if commit:
                    await db.commit()
        elif self._l2 is not None:
            yield self._l2
        else:
            raise CapabilityError(5004, "L2 仓储未装配（组合根未注册），该操作降级不可用")

    def namespace(self) -> str:
        return "memory"

    def capabilities(self) -> list[CapabilityDescriptor]:
        return [
            CapabilityDescriptor(
                name="memory.read",
                description=(
                    "记忆读取（session=L1 全量 / user=L2 检索；org/knowledge 恒空集，knowledge 走 knowledge.search）"
                ),
                required_scopes=("memory:read",),
                annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
                semantic={"x-ontology": {"memory_level_object_class": "ob2:MemoryFact"}},
            ),
            CapabilityDescriptor(
                name="memory.write",
                description="记忆写入（session=L1 块 / user=L2 候选事实——指纹幂等，候选非成品，进审核）",
                required_scopes=("memory:write",),
                annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
                semantic={
                    "x-ontology": {"write_policy": "candidate-first", "layer_authorization": "memory:write:{level}"}
                },
            ),
            CapabilityDescriptor(
                name="memory.invalidate",
                description=(
                    "失效标记（墓碑式软删：置 invalidated 并写 valid_to，不物理删除；reason 必填；"
                    "重复失效返回原状态；影子表归档随 K2-a）"
                ),
                required_scopes=("memory:write",),
                annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
                semantic={"x-ontology": {"irreversible": "tombstone", "physical_delete": False}},
            ),
        ]

    async def invoke(self, name: str, params: dict[str, Any], ctx: CallContext) -> CapabilityResult:
        local = name.split(".", 1)[1]
        if local == "read":
            return CapabilityResult.success(await self._read(params, ctx))
        if local == "write":
            return CapabilityResult.success(await self._write(params, ctx))
        if local == "invalidate":
            return CapabilityResult.success(await self._invalidate(params, ctx))
        raise CapabilityError(3001, f"能力未实现: {name}")

    async def _read(self, params: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        from datetime import UTC, datetime

        level = params.get("level")
        top_k = int(params.get("top_k") or 5)
        memories: list[dict[str, Any]] = []
        if level == "session":
            if self._l1 is None:
                raise CapabilityError(5004, "L1 存储未装配（组合根未注册），session 级读取降级不可用")
            session_id = _parse_uuid(params.get("session_id"), "session_id")
            snapshot = await self._l1.read(ctx.tenant_id, session_id)
            memories = [
                {"content": block.content, "layer": "session", "score": 1.0, "source": "l1", "key": block.key}
                for block in snapshot.blocks.values()
            ]
            memories += [
                {
                    "content": message.content,
                    "layer": "session",
                    "score": 0.9,
                    "source": "l1",
                    "key": f"window:{message.role}",
                }
                for message in snapshot.window[:top_k]
            ]
            return {"memories": memories[:top_k], "degraded": snapshot.degraded}
        if level == "user":
            async with self._l2_for(ctx) as repo:
                hits = await merge_l2_hits(
                    repo,
                    user_id=ctx.subject_id or ctx.tenant_id,
                    query=str(params.get("query") or ""),
                    mode="full",
                    top_k=top_k,
                    rrf_k=self._rrf_k,
                    half_life_days=self._half_life_days,
                    now=datetime.now(UTC),
                )
            memories = [
                {
                    "content": hit.content,
                    "layer": "user",
                    "score": hit.score,
                    "source": hit.source,
                    "fact_id": str(hit.fact_id),
                }
                for hit in hits
            ]
            return {"memories": memories}
        # org/knowledge：L3 M5 延后 / L4 走 knowledge.search（MCP 篇 §2 映射裁决）——恒空集占位
        return {"memories": [], "note": f"level={level} 不由本工具供给（L3 随 M5；L4 走 knowledge.search）"}

    async def _write(self, params: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        from datetime import UTC, datetime

        level = params.get("level")
        content = str(params.get("content") or "")
        if not content:
            raise CapabilityError(3001, "content 为必填")
        if level == "session":
            if self._l1 is None:
                raise CapabilityError(5004, "L1 存储未装配（组合根未注册），session 级写入降级不可用")
            session_id = _parse_uuid(params.get("session_id"), "session_id")
            key = f"mcp:{uuid.uuid4().hex[:12]}"
            applied = await self._l1.write_blocks(
                ctx.tenant_id,
                session_id,
                [MemoryBlock(key=key, title="; ".join(params.get("tags") or [])[:128], content=content)],
            )
            return {"fact_id": key, "status": "written", "applied": applied}
        if level == "user":
            user_id = ctx.subject_id or ctx.tenant_id
            category = FactCategory.FACT
            fingerprint = fact_fingerprint(content, category)
            async with self._l2_for(ctx, commit=True) as repo:
                existing = await repo.find_by_fingerprint(user_id, fingerprint)
                if existing is not None:  # 候选非成品 + 指纹幂等（memory §5.4）：重复提交返回原 fact_id
                    return {"fact_id": str(existing.id), "status": existing.status.value, "duplicate": True}
                source_session = (
                    _parse_uuid(params.get("session_id"), "session_id") if params.get("session_id") else None
                )
                fact = L2Fact(
                    id=uuid.uuid4(),
                    tenant_id=ctx.tenant_id,
                    user_id=user_id,
                    content=content,
                    category=category,
                    confidence=0.5,  # 候选置信度缺省（PoC②：自报置信度仅作排序/抽检依据）
                    decay_score=0.5,
                    source_session_id=source_session,
                    valid_from=datetime.now(UTC),
                )
                await repo.add(fact)
            return {"fact_id": str(fact.id), "status": fact.status.value, "duplicate": False}
        raise CapabilityError(3001, "level 仅允许 session|user（L3/L4 不对 agent 开放写，memory §5.2）")

    async def _invalidate(self, params: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        from datetime import UTC, datetime

        fact_id = _parse_uuid(params.get("fact_id"), "fact_id")
        # K2-a §11.1：reason 必填（与 memory api 同一领域红线，无 reason 拒绝失效）
        reason = str(params.get("reason") or "").strip()
        if not reason:
            raise CapabilityError(3001, "reason 必填（无 reason 拒绝失效，memory §11.1）")
        async with self._l2_for(ctx, commit=True) as repo:
            fact = await repo.get(fact_id)
            if fact is None:  # 未命中或跨租户一律不存在（不泄露存在性，memory api 同款口径）
                raise CapabilityError(3001, "记忆事实不存在")
            if fact.status.value != "invalidated":  # 幂等：已失效重复提交原样返回
                now = datetime.now(UTC)
                fact.invalidate(now, reason=reason)
                # 失效即归档：影子行与 save_state 同事务（§11.1 断链防线，api 层同款）
                await repo.archive_invalidated(fact, reason=reason, invalidated_at=now)
                await repo.save_state(fact)
        valid_to = fact.valid_to.isoformat() if fact.valid_to else None
        return {"fact_id": str(fact.id), "status": "invalidated", "valid_to": valid_to}


# ---------------------------------------------------------------- knowledge


class KnowledgeCapabilityProvider:
    """knowledge.search（api/03 §3.1）：检索协作对象以 KnowledgeSearchFn 协议注入（禁直 import kb）。"""

    def __init__(self, search_fn: KnowledgeSearchFn | None) -> None:
        self._search = search_fn
        self.provider_version = "0.1.0"
        self.supported_loop_versions = [KERNEL_LOOP_VERSION]

    def namespace(self) -> str:
        return "knowledge"

    def capabilities(self) -> list[CapabilityDescriptor]:
        return [
            CapabilityDescriptor(
                name="knowledge.search",
                description="GraphRAG 混合检索（auto/local/global/drift，LazyGraphRAG lite），返回引用与图路证据链",
                required_scopes=("kb:read",),
                annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
                semantic={"x-ontology": {"kb_scoped": True, "evidence": ["graph_paths", "citations"]}},
            ),
        ]

    async def invoke(self, name: str, params: dict[str, Any], ctx: CallContext) -> CapabilityResult:
        if self._search is None:
            raise CapabilityError(5004, "检索协作对象未装配（组合根未注册 KnowledgeSearchFn），search 降级不可用")
        kb_raw = params.get("kb_id")
        result = await self._search.search(
            tenant_id=ctx.tenant_id,
            query=str(params.get("query") or ""),
            kb_id=uuid.UUID(str(kb_raw)) if kb_raw else None,
            top_k=int(params.get("top_k") or 8),
            mode=str(params.get("mode") or "auto"),
            entity_type_filter=params.get("entity_type_filter") or None,
            with_evidence=bool(params.get("with_evidence", True)),
            acl_tags=_acl_tags_from_ctx(ctx),
        )
        return CapabilityResult.success(_project_search(result))


def _acl_tags_from_ctx(ctx: CallContext) -> Sequence[str] | None:
    """调用方 acl 标签面（OntRAG §4.3）：ctx.extra["acl_tags"]（通道/principal 扩展字段）。

    - 缺失/None → None：调用方未接入标签面，kb 侧谓词不激活（no-op，兼容红线）；
    - str → 逗号分隔解析（空串=显式空标签面，deny-by-default）；
    - 序列 → 逐项 str 化。
    标签面属授权上下文，只从 CallContext（网关侧信令断言）采集，禁从工具 params 采集
    （不可信输入不作授权依据，capability_provider 红线同源）。
    """
    raw = ctx.extra.get("acl_tags")
    if raw is None:
        return None
    if isinstance(raw, str):
        return [tag.strip() for tag in raw.split(",") if tag.strip()]
    return [str(tag) for tag in raw]


def _project_search(result: Any) -> dict[str, Any]:
    """检索结果 → api/03 §3.1 输出契约（duck-typed 投影，不 import kb 返回类型）。"""
    citations = []
    for c in getattr(result, "citations", []):
        citations.append(
            {
                "chunk_id": str(getattr(c, "chunk_id", "")),
                "doc_id": str(getattr(c, "doc_id", "")),
                "doc_name": getattr(c, "doc_name", None),
                "quote": getattr(c, "quote", ""),
                "span": getattr(c, "span", None),
                "score": getattr(c, "score", 0.0),
            }
        )
    graph_paths = list(getattr(result, "graph_paths", []) or [])
    answers = [
        {
            "summary": f"命中 {len(citations)} 条引用（degraded={getattr(result, 'degraded', False)}）",
            "confidence": max((float(c.get("score") or 0.0) for c in citations), default=0.0),
            "evidence": {"graph_paths": graph_paths, "citations": citations},
        }
    ]
    return {
        "answers": answers,
        "hits": citations,
        "graph_paths": graph_paths,
        "citations": citations,
        "confidence": answers[0]["confidence"],
        "degraded": bool(getattr(result, "degraded", False)),
    }


def _parse_uuid(value: Any, field: str) -> uuid.UUID:
    if value is None:
        raise CapabilityError(3001, f"{field} 为必填")
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise CapabilityError(3001, f"{field} 非法 UUID: {value}") from exc


# ---------------------------------------------------------------- action（4.2 回写批次追加）


@runtime_checkable
class ActionDispatcherPort(Protocol):
    """action_dispatcher 执行面协议（形状=writeback.business.ActionDispatcher 公开面）。

    mcp 禁入 writeback 内部分层：幂等键/台账状态机/对账补偿全在 dispatcher（业务回写设计
    权威），本 provider 只做入参投影与错误码映射——组合根绑定实现（07 §1 依赖倒置同款）。
    """

    async def invoke_action(
        self,
        *,
        tenant_id: uuid.UUID,
        action_iri: str,
        params: dict[str, Any],
        confirm_token: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]: ...  # pragma: no cover — Protocol 方法无实现


class ActionCapabilityProvider:
    """action.invoke（api/03 §3.7/§7）：业务回写出口——受理即凭证，重复请求返回原结果。

    dispatcher 未装配时结构化降级 5003（与其他 provider 的组合根欠账口径一致）；
    域错误（WritebackError）→ CapabilityResult.failure（code=已登记平台错误码，api/03 §8）。
    """

    def __init__(self, dispatcher: ActionDispatcherPort | None = None) -> None:
        self._dispatcher = dispatcher
        self.provider_version = "0.1.0"
        self.supported_loop_versions = [KERNEL_LOOP_VERSION]

    def namespace(self) -> str:
        return "action"

    def capabilities(self) -> list[CapabilityDescriptor]:
        # description/annotations 与出口静态权威 TOOL_SPECS["action.invoke"] 逐字一致
        # （一致性由 tests 断言；server.py 未注册 provider 时回落该静态权威）。
        return [
            CapabilityDescriptor(
                name="action.invoke",
                description=(
                    "业务动作回写（须为本体行动类 IRI；受理即凭证，高风险须 confirm_token——4.2 批次装配执行面）"
                ),
                required_scopes=("action:invoke",),
                annotations={
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": True,
                },
                semantic={
                    "x-ontology": {
                        "action_iri": None,
                        "object_class": None,
                        "guard_rules": [],
                        "risk_level": "high",
                        "required_scopes": ["action:invoke"],
                    }
                },
            )
        ]

    async def invoke(self, name: str, params: dict[str, Any], ctx: CallContext) -> CapabilityResult:
        from services.platform.errors import ErrorCode
        from services.writeback.domain.model import WritebackError

        local = name.split(".", 1)[1]
        if local != "invoke":
            raise CapabilityError(3001, f"能力未实现: {name}")
        if self._dispatcher is None:
            raise CapabilityError(
                int(ErrorCode.MCP_TARGET_UNAVAILABLE),
                "action.invoke 执行面未装配（组合根未注册 action_dispatcher），降级不可用",
            )
        try:
            value = await self._dispatcher.invoke_action(
                tenant_id=ctx.tenant_id,
                action_iri=str(params.get("action_iri") or ""),
                params=dict(params.get("params") or {}) if isinstance(params.get("params"), dict) else {},
                confirm_token=params.get("confirm_token") or None,
                trace_id=ctx.trace_id,
            )
        except WritebackError as exc:
            return CapabilityResult.failure(exc.code, exc.message)
        return CapabilityResult.success(value)


@runtime_checkable
class WritebackStatusPort(Protocol):
    """writeback.status 查询面协议（形状=writeback.business.ActionDispatcher.status 公开面）。

    mcp 零 writeback 内部分层 import（与 ActionDispatcherPort 同款倒置）：三键定位/租户过滤/
    404 语义/主动核实全在 dispatcher（api/03 §3.9 权威），本 provider 只做入参投影与错误映射。
    """

    async def status(
        self,
        *,
        tenant_id: uuid.UUID,
        ledger_id: str | uuid.UUID | None = None,
        idempotency_key: str | None = None,
        action_instance_id: str | uuid.UUID | None = None,
    ) -> dict[str, Any]: ...  # pragma: no cover — Protocol 方法无实现


class WritebackStatusCapabilityProvider:
    """writeback.status（api/03 §3.9/§2 ★ 本篇补充）：受理凭证兑现通道——查询回写台账状态。

    输入三键任一（ledger_id / idempotency_key / action_instance_id，anyOf）→ 台账全字段
    （tenant 过滤；未找到 404 语义按 api/03 §2）；scope=action:invoke（api/03 §2 登记口径，
    独立 scope 待上游定稿）。查询面只读幂等；非终态主动核实语义在 dispatcher.status。
    """

    def __init__(self, status_port: WritebackStatusPort | None = None) -> None:
        self._status = status_port
        self.provider_version = "0.1.0"
        self.supported_loop_versions = [KERNEL_LOOP_VERSION]

    def namespace(self) -> str:
        return "writeback"

    def capabilities(self) -> list[CapabilityDescriptor]:
        return [
            CapabilityDescriptor(
                name="writeback.status",
                description=(
                    "回写台账状态查询（凭 action.invoke 受理凭证三键任一定位；tenant 过滤；只读幂等，未找到 404 语义）"
                ),
                required_scopes=("action:invoke",),
                annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
                semantic={"x-ontology": {"ledger": "writeback_ledger", "not_found_code": 404}},
            )
        ]

    async def invoke(self, name: str, params: dict[str, Any], ctx: CallContext) -> CapabilityResult:
        from services.platform.errors import ErrorCode
        from services.writeback.domain.model import WritebackError

        local = name.split(".", 1)[1] if "." in name else name
        if local != "status":
            raise CapabilityError(3001, f"能力未实现: {name}")
        if self._status is None:
            raise CapabilityError(
                int(ErrorCode.MCP_TARGET_UNAVAILABLE),
                "writeback.status 查询面未装配（组合根未注册 status_port），降级不可用",
            )
        keys = [k for k in ("ledger_id", "idempotency_key", "action_instance_id") if params.get(k)]
        if not keys:  # api/03 §3.9 inputSchema anyOf：三键至少其一
            raise CapabilityError(3001, "须提供 ledger_id / idempotency_key / action_instance_id 之一")
        try:
            value = await self._status.status(
                tenant_id=ctx.tenant_id,
                ledger_id=params.get("ledger_id"),
                idempotency_key=params.get("idempotency_key"),
                action_instance_id=params.get("action_instance_id"),
            )
        except WritebackError as exc:
            return CapabilityResult.failure(exc.code, exc.message)
        return CapabilityResult.success(value)
