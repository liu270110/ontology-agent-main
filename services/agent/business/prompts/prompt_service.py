"""提示词模板库用例服务（H-1；api/01 §5.10 五端点 + 运行时钉死解析消费面）。

纪律（与 approval_service 同源）：全部写路径经 UoW+聚合方法（禁裸 SQL、禁绕过聚合
直改 status/append 版本）；领域 PromptError → GatewayError 映射复用 resolver 同款
（消息前缀即登记码）。权限面：scope 门禁在端点（require_scope），**owner 校验在本
服务**——personal 作用域仅 owner 可写（其余 2002）；tenant 作用域凭 prompt:write。

运行期引用记录（版本钉死纪律）：build_db_prompt_resolver 装配的解析器在消解事务内
落 outbox 事件 ``prompt.ref_resolved``（id@version+checksum+consumer，06 §1/§8——
与业务行同事务，relay 异步发布；消费接线 v1=builtin 钉死引用一处）。
"""

from __future__ import annotations

import uuid

from services.agent.business.prompts.resolver import (
    ResolvedPrompt,
    format_prompt_ref,
    parse_prompt_ref,
    prompt_error_to_gateway,
    resolved_from_version,
)
from services.agent.domain.model.prompt import (
    FewShotExample,
    PromptError,
    PromptScope,
    PromptStatus,
    PromptTemplate,
    PromptVersion,
    VariableDecl,
    compute_checksum,
)
from services.platform.errors import GatewayError

_REF_RESOLVED_EVENT = "prompt.ref_resolved"  # outbox：运行期钉死引用记录（id@version+checksum）


class PromptLibraryService:
    """提示词模板库用例（api/01 §5.10）：CRUD + 钉死解析；仓储实例只从 UoW 获取。"""

    def __init__(self, uow: object) -> None:  # AsyncUnitOfWork（鸭子访问防循环 import，同 approval_service）
        self._uow = uow

    # ── POST /prompts：创建（含首版本 v1）────────────────────────────────────
    async def create(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        scope: PromptScope | str,
        slug: str,
        name: str,
        system_prompt: str | None = None,
        template: str | None = None,
        few_shot: list[FewShotExample] | None = None,
        variables: list[VariableDecl] | None = None,
    ) -> PromptTemplate:
        try:
            draft = PromptTemplate.create(
                tenant_id=tenant_id,
                owner_user_id=actor_user_id,  # 创建者即 owner（personal 作用域写权限锚点）
                scope=scope,
                slug=slug,
                name=name,
                system_prompt=system_prompt,
                template=template,
                few_shot=few_shot or [],
                variables=variables or [],
                created_by=actor_user_id,
            )
            async with self._uow.for_tenant(tenant_id) as tx:
                await self._ensure_slug_free(tx, scope=draft.scope, slug=draft.slug)
                await tx.prompts.add(draft)
        except PromptError as exc:
            raise prompt_error_to_gateway(exc) from exc
        return draft

    # ── GET /prompts：列表（scope/name 过滤+分页；personal 仅 owner 可见）────────
    async def list(
        self,
        *,
        tenant_id: uuid.UUID,
        viewer_user_id: uuid.UUID,
        scope: PromptScope | None = None,
        name: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[PromptTemplate], int]:
        async with self._uow.for_tenant(tenant_id) as tx:
            items = await tx.prompts.list(
                scope=scope, name=name, status=PromptStatus.ACTIVE,
                viewer_user_id=viewer_user_id, offset=offset, limit=limit,
            )
            total = await tx.prompts.count(
                scope=scope, name=name, status=PromptStatus.ACTIVE, viewer_user_id=viewer_user_id
            )
        return items, total

    # ── GET /prompts/{id}：详情（含版本树）────────────────────────────────────
    async def get(
        self, *, tenant_id: uuid.UUID, viewer_user_id: uuid.UUID, template_id: uuid.UUID
    ) -> PromptTemplate:
        async with self._uow.for_tenant(tenant_id) as tx:
            found = await tx.prompts.get(template_id)
        self._check_visible(found, viewer_user_id=viewer_user_id)
        if found.status is PromptStatus.ARCHIVED:
            # DELETE=软删：REST 呈现面按已删除（404）；行保留可追溯（uk 名额不释放）
            raise GatewayError(404, "提示词模板不存在", status_code=404)
        return found

    # ── PUT /prompts/{id}：语义=追加新版本（名字/内容变更都产新版本，版本化红线）────
    async def update(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        template_id: uuid.UUID,
        name: str | None = None,
        system_prompt: str | None = None,
        template: str | None = None,
        few_shot: list[FewShotExample] | None = None,
        variables: list[VariableDecl] | None = None,
    ) -> PromptTemplate:
        try:
            async with self._uow.for_tenant(tenant_id) as tx:
                found = await tx.prompts.get(template_id)
                self._check_writable(found, actor_user_id=actor_user_id)
                found.draft_new_version(
                    name=name,
                    system_prompt=system_prompt,
                    template=template,
                    few_shot=few_shot,
                    variables=variables,
                    created_by=actor_user_id,
                )
                await tx.prompts.save(found)
        except PromptError as exc:
            raise prompt_error_to_gateway(exc) from exc
        return found

    # ── DELETE /prompts/{id}：archive 软删（204；已归档幂等成功）──────────────────
    async def archive(self, *, tenant_id: uuid.UUID, actor_user_id: uuid.UUID, template_id: uuid.UUID) -> None:
        try:
            async with self._uow.for_tenant(tenant_id) as tx:
                found = await tx.prompts.get(template_id)
                self._check_writable(found, actor_user_id=actor_user_id)
                if found.status is PromptStatus.ARCHIVED:
                    return  # 幂等：重复 DELETE 仍 204（终态保持）
                found.archive()
                await tx.prompts.save(found)
        except PromptError as exc:
            raise prompt_error_to_gateway(exc) from exc

    # ── 运行时钉死解析（消费面：builtin 适配器 / 任务定义 template_ref）──────────
    async def resolve_pinned(
        self,
        *,
        tenant_id: uuid.UUID,
        ref: str,
        consumer: str = "api",
    ) -> ResolvedPrompt:
        """``prompt:{id}@{version|head}`` → 确定版本回执（带 checksum；@head 消解为具体号）。

        防线：archived 模板禁止引用（409）；checksum 回执重算比对（防御错载，正常不可能
        失配——版本行只插入不更新）；consumer 标注引用来源（审计/事件归因口径）。
        """
        parsed = self._parse(ref)
        async with self._uow.for_tenant(tenant_id) as tx:
            found = await tx.prompts.get(parsed.template_id)
            if found is None:
                raise GatewayError(404, f"提示词模板不存在: {parsed.template_id}", status_code=404)
            try:
                found.ensure_usable()
                version = found.head if parsed.version is None else found.resolve(parsed.version)
            except PromptError as exc:
                raise prompt_error_to_gateway(exc) from exc
            self._verify_checksum(found, version)
            tx.enqueue_projection(
                _REF_RESOLVED_EVENT,
                found.id,
                {
                    "ref": format_prompt_ref(found.id, version.version),
                    "slug": found.slug,
                    "version": version.version,
                    "checksum": version.checksum,
                    "consumer": consumer,
                },
            )
            return resolved_from_version(template_id=found.id, slug=found.slug, version=version)

    # ── 内部 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _parse(ref: str):
        """纯解析 + 领域错误→网关错误映射（违式引用 3001/400，不让 PromptError 裸穿）。"""
        try:
            return parse_prompt_ref(ref)
        except PromptError as exc:
            raise prompt_error_to_gateway(exc) from exc

    async def _ensure_slug_free(self, tx: object, *, scope: PromptScope, slug: str) -> None:
        """slug 预检（含 archived——uk 全状态生效，归档模板占用 slug 名额不释放）。"""
        if await tx.prompts.get_by_slug(scope=scope, slug=slug) is not None:
            raise GatewayError(409, f"slug 已存在于该作用域: {slug}", status_code=409)

    def _check_visible(self, found: PromptTemplate | None, *, viewer_user_id: uuid.UUID) -> None:
        """可见性：不存在/跨租户=404；personal 非 owner=404（不泄露存在性）。"""
        if found is None:
            raise GatewayError(404, "提示词模板不存在", status_code=404)
        if found.scope is PromptScope.PERSONAL and found.owner_user_id != viewer_user_id:
            raise GatewayError(404, "提示词模板不存在", status_code=404)

    def _check_writable(self, found: PromptTemplate | None, *, actor_user_id: uuid.UUID) -> None:
        """写权限（owner 校验，api/01 §5.10）：personal 仅 owner（否则 2002）；tenant 凭 prompt:write。"""
        if found is None:
            raise GatewayError(404, "提示词模板不存在", status_code=404)
        if found.scope is PromptScope.PERSONAL and found.owner_user_id != actor_user_id:
            raise GatewayError(2002, "personal 作用域模板仅 owner 可变更", status_code=403)

    def _verify_checksum(self, found: PromptTemplate, version: PromptVersion) -> None:
        """checksum 回执重算比对（确定性防线；失配=存储内容与版本指纹不一致，拒载）。"""
        recomputed = compute_checksum(
            system_prompt=version.system_prompt,
            template=version.template,
            few_shot=version.few_shot,
            variables=version.variables,
        )
        if recomputed != version.checksum:
            raise GatewayError(
                5005,
                f"提示词版本 checksum 失配: {found.slug}@{version.version}"
                f"（行内 {version.checksum} ≠ 重算 {recomputed}，疑似错载，拒绝解析）",
                status_code=500,
            )


def build_db_prompt_resolver(uow: object, *, tenant_id: uuid.UUID, consumer: str = "builtin") -> object:
    """DB 装配的运行时解析器（组合根/编排器注入 builtin 用；PromptResolver 协议实现）。

    每次消解=一个短事务：读模板+版本 → 校验 → 落 ``prompt.ref_resolved`` outbox 事件
    （id@version+checksum，06 §1 与业务行同事务）→ 返回回执。组合接线（chat_orchestrator
    装配面注入 BuiltinAdapter）不属本批领地——遗留登记见批次报告。
    """
    service = PromptLibraryService(uow)

    async def _resolve(ref: str) -> ResolvedPrompt:
        return await service.resolve_pinned(tenant_id=tenant_id, ref=ref, consumer=consumer)

    return _resolve


def build_tenant_ctx_prompt_resolver(session_factory: object, *, consumer: str = "builtin") -> object:
    """应用级装配的租户迟绑定解析器（chat_orchestrator 组合根注入 builtin 用，H-1 接线）。

    编排器/适配器为应用级单例而模板库按租户隔离——本闭包在**每次消解时**从
    ``tenant_id_ctx``（08 §1，请求/worker 侧已注入的同一 ContextVar）取当前租户并
    开短事务解析；无租户上下文（后台任务断链）时 fail-closed 抛 GatewayError(5002)
    ——不猜测默认租户（多租户红线）。
    """
    from services.platform.errors import GatewayError, tenant_id_ctx

    async def _resolve(ref: str) -> ResolvedPrompt:
        tenant = tenant_id_ctx.get()
        if not tenant:
            raise GatewayError(5002, f"prompt: 钉死引用解析无租户上下文（fail-closed）: {ref}", status_code=503)
        from services.platform.db.uow import AsyncUnitOfWork

        uow = AsyncUnitOfWork(session_factory)  # type: ignore[arg-type]
        service = PromptLibraryService(uow)
        return await service.resolve_pinned(tenant_id=uuid.UUID(tenant), ref=ref, consumer=consumer)

    return _resolve
