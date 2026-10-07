"""L2 adapter-profiles 路由（docs/Agent/20 §4：Bridge profile 管理面最小读端点）。

GET /adapter-profiles：列出可用 profile（id/tool/form/protocol/capabilities）——前端 agent
创建时下拉选（自定义接入=写一份 YAML，不写一行 Python，05 篇 §5.1）。{data,meta} 信封
（api/01 §3.1 统一信封口径）。profile 上传/审核（集市 skills 同款审核链）列下波（20 篇 §7）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from services.agent.business.adapters.profiles import scan_profiles
from services.platform.deps import Principal, require_scope

router = APIRouter(prefix="/adapter-profiles", tags=["agents"])

ProfileReadDep = Annotated[Principal, Depends(require_scope("agent:read"))]


class AdapterProfileOut(BaseModel):
    """profile 列表项（05 篇 §5.2 字段的读面子集：发现面只暴露声明，不暴露端点/凭据）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    tool: str
    form: str
    protocol: str
    profile_version: int
    capabilities: dict[str, bool]


class AdapterProfileListOut(BaseModel):
    """{data, meta} 信封（api/01 §3.1；meta.page_size 缺省=返回全部——内置 profile 量级为个位数）。"""

    model_config = ConfigDict(extra="forbid")

    data: list[AdapterProfileOut]
    meta: dict[str, Any]


@router.get("", summary="内置 agent profile 清单（form/capabilities 声明面；20 篇 §4）")
async def list_adapter_profiles(
    principal: ProfileReadDep,
    form: Annotated[str | None, Query(pattern="^(F2|F3)$")] = None,
    root: Annotated[str | None, Query(max_length=512, include_in_schema=False)] = None,
) -> AdapterProfileListOut:
    """profile 目录扫描（内置 adapters/profiles/；Settings.agent_adapter_profiles_root 追加覆盖
    经组合根生效，本端点 root 仅测试注桩通道）。装载失败条目跳过（fail-soft，扫描面口径）。"""
    profiles = scan_profiles(root)
    items = [
        AdapterProfileOut(
            id=p.profile,
            tool=p.tool,
            form=p.form,
            protocol=p.protocol,
            profile_version=p.profile_version,
            capabilities=dict(sorted(p.capabilities.items())),
        )
        for p in sorted(profiles.values(), key=lambda x: x.profile)
        if form is None or p.form == form
    ]
    return AdapterProfileListOut(data=items, meta={"total": len(items)})
