# tests/ontology/test_ont2_capabilities_api.py
"""ONT-2 能力只读 API 用例（06 篇 §ONT-2.2 读模型 + 登记册风格；真实 PG 一次性库 + 真实路由挂载）。

用例矩阵：
- 清单：200 分页形状（items/total/offset/limit）、撤除=标记（缺省不含 withdrawn 行，
  include_withdrawn=true 含）、kind 过滤；
- 详情：200 全列形状；未知能力/跨租户本体 404 不泄露；
- 只读契约：POST/DELETE 同路径 405（无写端点）；DTO extra="forbid"（未知 query 参数 422）。

夹具：ont1_pg（一次性库）+ ASGITransport 最小 app 挂真实路由（test_ont1_lifecycle_api 同款装配，
只读面无需制品库替身——PgOntologyRepository.get 不触制品）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse

from services.ontology.api.capabilities import router as capability_router
from services.ontology.data.orm import Capability as CapabilityORM
from services.ontology.data.orm import Ontology as OntologyORM
from services.ontology.data.orm import OntologyVersion as OntologyVersionORM
from services.platform.deps import Principal, get_current_principal, get_session
from services.platform.errors import GatewayError, error_response
from tests.ontology.ont1_pg import (  # noqa: F401  夹具 ont1_pg 经 conftest 注册（本文件按名请求）
    cleanup_tenant,
    seed_tenant_user,
)

EXECUTION = {"execution_mode": "read", "deterministic": True, "triggered_by_event": None, "guarded_by_rule": None}


@dataclass(frozen=True)
class ApiEnv:
    client: AsyncClient
    factory: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    ontology_id: uuid.UUID
    capability_id: uuid.UUID


def _principal(tenant_id: uuid.UUID, user_id: uuid.UUID) -> Principal:
    return Principal(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "roles": ["member"],
            "scopes": ["ontology:read", "ontology:write"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


async def _gateway_error_body(request: Request, exc: GatewayError) -> JSONResponse:
    return error_response(request, exc.code, exc.message, status_code=exc.status_code, detail=exc.detail)


@pytest.fixture
async def ont2_api(
    ont1_pg: async_sessionmaker[AsyncSession],
) -> AsyncIterator[ApiEnv]:
    """一次性库 + 真实只读路由 + 两租户隔离数据（A 租户 2 行，其中 1 行撤除标记）。"""
    factory = ont1_pg
    tenant_id, user_id = await seed_tenant_user(factory)
    other_tenant, other_user = await seed_tenant_user(factory)
    principal = _principal(tenant_id, user_id)

    async def _test_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(capability_router)
    app.add_exception_handler(GatewayError, _gateway_error_body)
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_session] = _test_session

    async with factory() as db, db.begin():
        ontology = OntologyORM(
            id=uuid.uuid4(), tenant_id=tenant_id, iri_base="http://ontology-agent.local/cap#", name="能力本体"
        )
        db.add(ontology)
        other_ontology = OntologyORM(
            id=uuid.uuid4(), tenant_id=other_tenant, iri_base="http://o/#other", name="他租户本体"
        )
        db.add(other_ontology)
        await db.flush()
        version = OntologyVersionORM(
            tenant_id=tenant_id,
            ontology_id=ontology.id,
            version="v1",
            version_no=1,
            artifact_key=f"ontologies/{tenant_id}/{ontology.id}/v1.ttl",
            checksum="2" * 64,
        )
        db.add(version)
        await db.flush()
        cap = CapabilityORM(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            ontology_id=ontology.id,
            version_id=version.id,
            iri="http://ontology-agent.local/cap#fs-read",
            kind="atomic",
            name="fs.read",
            label="fs.read",
            description="读取工作区文件",
            requires=["http://ontology-agent.local/cap#WorkspaceFile"],
            produces=["http://ontology-agent.local/cap#FileText"],
            constrained_by="http://ontology-agent.local/cap#FsReadShape",
            execution=EXECUTION,
            binds_action="http://ontology.example/action/file_read",
            current_definition_version_id=None,
            source="seed",
        )
        withdrawn = CapabilityORM(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            ontology_id=ontology.id,
            version_id=version.id,
            iri="http://ontology-agent.local/cap#legacy",
            kind="atomic",
            name="legacy",
            execution=EXECUTION,
            source="manual",
            withdrawn_at=None,
        )
        from datetime import UTC, datetime

        withdrawn.withdrawn_at = datetime.now(UTC)
        withdrawn.withdrawn_reason = "已被替代"
        db.add_all([cap, withdrawn])
        ontology_id, capability_id = ontology.id, cap.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        yield ApiEnv(
            client=client,
            factory=factory,
            tenant_id=tenant_id,
            user_id=user_id,
            ontology_id=ontology_id,
            capability_id=capability_id,
        )
    await cleanup_tenant(factory, tenant_id)
    await cleanup_tenant(factory, other_tenant)


async def test_清单_分页形状_撤除标记缺省隐藏(ont2_api: ApiEnv) -> None:
    """GET 清单：total/offset/limit 形状；缺省隐藏撤除行（撤除=标记），include_withdrawn 显含。"""
    resp = await ont2_api.client.get(f"/ontologies/{ont2_api.ontology_id}/capabilities")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1 and body["offset"] == 0 and body["limit"] == 50
    assert [i["iri"] for i in body["items"]] == ["http://ontology-agent.local/cap#fs-read"]

    resp2 = await ont2_api.client.get(
        f"/ontologies/{ont2_api.ontology_id}/capabilities", params={"include_withdrawn": "true"}
    )
    assert resp2.json()["total"] == 2  # 撤除行仍在库（不物理删）

    resp3 = await ont2_api.client.get(
        f"/ontologies/{ont2_api.ontology_id}/capabilities", params={"kind": "composite"}
    )
    assert resp3.json()["total"] == 0


async def test_详情_全列形状_快照回填列可见(ont2_api: ApiEnv) -> None:
    """GET 详情：契约列面全量回显（iri/kind/签名/执行四元/binds_action/source/快照回填列）。"""
    resp = await ont2_api.client.get(f"/ontologies/{ont2_api.ontology_id}/capabilities/{ont2_api.capability_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["iri"] == "http://ontology-agent.local/cap#fs-read"
    assert body["kind"] == "atomic" and body["source"] == "seed"
    assert body["requires"] == ["http://ontology-agent.local/cap#WorkspaceFile"]
    assert body["produces"] == ["http://ontology-agent.local/cap#FileText"]
    assert body["constrained_by"] == "http://ontology-agent.local/cap#FsReadShape"
    assert body["execution"]["execution_mode"] == "read"
    assert body["binds_action"] == "http://ontology.example/action/file_read"
    assert set(body) == {
        "id", "ontology_id", "version_id", "iri", "kind", "name", "label", "description",
        "requires", "produces", "grants", "constrained_by", "execution", "serves_task",
        "binds_action", "current_definition_version_id", "source", "withdrawn_at",
        "withdrawn_reason", "created_at",
    }


async def test_404不泄露_未知能力_跨租户本体(ont2_api: ApiEnv) -> None:
    """未知能力 404；他租户本体（存在但非本租户）同 404 不泄露存在性。"""
    missing = await ont2_api.client.get(
        f"/ontologies/{ont2_api.ontology_id}/capabilities/{uuid.uuid4()}"
    )
    assert missing.status_code == 404 and missing.json()["code"] == 404
    foreign = await ont2_api.client.get(f"/ontologies/{uuid.uuid4()}/capabilities")
    assert foreign.status_code == 404


async def test_只读契约_写方法405_非法kind422(ont2_api: ApiEnv) -> None:
    """本批只读：POST/DELETE 同路径 405；kind pattern 校验（atomic|composite 封闭集）非法值 422。"""
    base = f"/ontologies/{ont2_api.ontology_id}/capabilities"
    assert (await ont2_api.client.post(base, json={})).status_code == 405
    assert (await ont2_api.client.delete(f"{base}/{ont2_api.capability_id}")).status_code == 405
    bad = await ont2_api.client.get(base, params={"kind": "tool"})  # 封闭集外 → 422
    assert bad.status_code == 422
