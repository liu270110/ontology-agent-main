"""L2 prompts 路由（H-1 提示词工程治理批；api/01 §5.10 预登记五端点兑现，F-08/X12）。

契约（严格按 §5.10 行）：
- GET  /prompts        列表（scope/name 过滤+分页；personal 仅 owner 可见）  prompt:read  200
- POST /prompts        新建（201，含首版本 v1）                                prompt:write 201（错 3001）
- GET  /prompts/{id}   详情（含版本树）                                        prompt:read  200（错 404*）
- PUT  /prompts/{id}   **语义=追加新版本**（名字/内容变更都产新版本，版本化红线；
                       200 返回新版本号；personal 仅 owner→2002）              prompt:write 200（错 404*、2002）
- DELETE /prompts/{id} archive 软删（204，幂等）                               prompt:write 204（错 404*、2002）

纪律：路由只做装配与 {data,meta} 信封；全部写路径经 UoW+聚合方法（版本不可变红线），
用例与 owner 校验在 business/prompts/prompt_service；scope 门禁=require_scope 依赖。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from services.agent.api.deps import UowDep
from services.agent.api.schemas.prompt import (
    PromptCreateIn,
    PromptUpdateIn,
    few_shot_to_domain,
    prompt_detail_from_domain,
    prompt_from_domain,
    variables_to_domain,
)
from services.agent.business.prompts.prompt_service import PromptLibraryService
from services.agent.domain.model.prompt import PromptScope
from services.platform.deps import Principal, require_scope

router = APIRouter(prefix="/prompts", tags=["prompts"])

PromptReadDep = Annotated[Principal, Depends(require_scope("prompt:read"))]
PromptWriteDep = Annotated[Principal, Depends(require_scope("prompt:write"))]


@router.get("", summary="提示词模板列表（scope/name 过滤+分页；personal 仅 owner 可见）")
async def list_prompts(
    principal: PromptReadDep,
    uow: UowDep,
    scope: Annotated[str | None, Query(pattern="^(personal|tenant)$")] = None,
    name: Annotated[str | None, Query(max_length=128)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    service = PromptLibraryService(uow)
    items, total = await service.list(
        tenant_id=principal.tenant_id,
        viewer_user_id=principal.user_id,
        scope=PromptScope(scope) if scope else None,
        name=name,
        offset=offset,
        limit=limit,
    )
    return {
        "data": [prompt_from_domain(t).model_dump() for t in items],
        "meta": {"offset": offset, "limit": limit, "total": total},
    }


@router.post("", status_code=status.HTTP_201_CREATED, summary="新建提示词模板（含首版本 v1）")
async def create_prompt(body: PromptCreateIn, principal: PromptWriteDep, uow: UowDep) -> dict:
    service = PromptLibraryService(uow)
    created = await service.create(
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        scope=body.scope,
        slug=body.slug,
        name=body.name,
        system_prompt=body.system_prompt,
        template=body.template,
        few_shot=few_shot_to_domain(body.few_shot),
        variables=variables_to_domain(body.variables),
    )
    return {"data": prompt_detail_from_domain(created).model_dump(), "meta": {}}


@router.get("/{template_id}", summary="模板详情（含版本树：内容/checksum/溯源）")
async def get_prompt(template_id: uuid.UUID, principal: PromptReadDep, uow: UowDep) -> dict:
    service = PromptLibraryService(uow)
    found = await service.get(
        tenant_id=principal.tenant_id, viewer_user_id=principal.user_id, template_id=template_id
    )
    return {"data": prompt_detail_from_domain(found).model_dump(), "meta": {}}


@router.put("/{template_id}", summary="追加新版本（名字/内容变更都产新版本——版本化红线；200 返回新版本号）")
async def update_prompt(template_id: uuid.UUID, body: PromptUpdateIn, principal: PromptWriteDep, uow: UowDep) -> dict:
    service = PromptLibraryService(uow)
    updated = await service.update(
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        template_id=template_id,
        name=body.name,
        system_prompt=body.system_prompt,
        template=body.template,
        few_shot=few_shot_to_domain(body.few_shot),
        variables=variables_to_domain(body.variables),
    )
    return {"data": prompt_detail_from_domain(updated).model_dump(), "meta": {"version": updated.head.version}}


@router.delete(
    "/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="归档软删（archive 终态，版本链保留可追溯；幂等）",
)
async def archive_prompt(template_id: uuid.UUID, principal: PromptWriteDep, uow: UowDep) -> None:
    service = PromptLibraryService(uow)
    await service.archive(tenant_id=principal.tenant_id, actor_user_id=principal.user_id, template_id=template_id)
