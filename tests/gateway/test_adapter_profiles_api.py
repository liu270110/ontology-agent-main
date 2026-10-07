# tests/gateway/test_adapter_profiles_api.py
"""adapter-profiles 端点测试（docs/Agent/20 §4：Bridge profile 管理面最小读端点）。

覆盖：GET /api/v1/adapter-profiles——{data,meta} 信封形状（api/01 §3.1）、四个内置 profile
（id/form/protocol/capabilities 声明面）、form 过滤、组合根挂载（路由表离线反射，不触 lifespan）。
端点零 DB 依赖（profile 目录扫描面）——端点函数直调（test_agents.py 同款风格）。
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI

from services.agent.api.adapter_profiles import AdapterProfileListOut, list_adapter_profiles
from services.gateway.app import create_app
from services.platform.config import Settings
from services.platform.deps import Principal

BUILTIN_IDS = {"openai-compatible", "nanobot-gateway", "pi-jsonl", "text-tail"}


def _principal() -> Principal:
    """agent:read 主体（api/01 §5.1 读面 scope；端点直调不触发 JWT 中间件）。"""
    return Principal(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(uuid.uuid4()),
            "roles": ["member"],
            "scopes": ["agent:read"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


async def test_profiles端点_信封形状与四个内置profile声明面() -> None:
    """{data,meta} 信封；四内置 profile 齐备；capabilities 为声明面（端点不暴露端点/凭据）。"""
    out: AdapterProfileListOut = await list_adapter_profiles(_principal(), form=None, root=None)

    assert set(out.model_dump()) == {"data", "meta"}  # api/01 §3.1 统一信封
    assert out.meta == {"total": len(out.data)}
    ids = {item.id for item in out.data}
    assert BUILTIN_IDS <= ids
    by_id = {item.id: item for item in out.data}
    assert by_id["openai-compatible"].form == "F3" and by_id["openai-compatible"].tool == "http-generic"
    assert by_id["nanobot-gateway"].protocol == "openai-chat"
    assert by_id["pi-jsonl"].form == "F2" and by_id["pi-jsonl"].profile_version == 1
    assert set(by_id["text-tail"].capabilities) == {"feed", "approval", "resume", "artifacts"}
    # 读面最小化：序列化里无 transport/endpoints/auth（发现面只暴露声明）
    assert "transport" not in out.data[0].model_dump()


async def test_profiles端点_form过滤_F2_F3() -> None:
    """form=F3 仅服务形态两条；form=F2 仅 CLI 两条（前端 agent 创建下拉按形态分流）。"""
    f3: AdapterProfileListOut = await list_adapter_profiles(_principal(), form="F3", root=None)
    f2: AdapterProfileListOut = await list_adapter_profiles(_principal(), form="F2", root=None)
    assert {i.form for i in f3.data} == {"F3"}
    assert {i.form for i in f2.data} == {"F2"}
    assert {i.id for i in f3.data} == {"openai-compatible", "nanobot-gateway"}
    assert {i.id for i in f2.data} == {"pi-jsonl", "text-tail"}


async def test_profiles端点_root注桩_空目录返回空集() -> None:
    """root 注桩（测试通道）：空目录 → data=[] 且信封完整（fail-soft 口径）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        out: AdapterProfileListOut = await list_adapter_profiles(_principal(), form=None, root=d)
    assert out.data == [] and out.meta == {"total": 0}


def test_组合根_挂载于_api_v1_前缀() -> None:
    """组合根挂载（gateway/app.py include_router）：离线 OpenAPI 反射（不触发 lifespan）。"""
    app: FastAPI = create_app(Settings(api_prefix="/api/v1"))
    assert "/api/v1/adapter-profiles" in app.openapi()["paths"]
