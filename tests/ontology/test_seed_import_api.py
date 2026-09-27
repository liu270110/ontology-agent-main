# tests/ontology/test_seed_import_api.py
"""种子本体导入 API 用例（POST /ontologies/import-seed：禁空工作台冷启动正式入口）。

覆盖：
- 导入成功：201 + 项目摘要（published/head=v1）+ 版本指针与制品 checksum 一致 + 制品内容含种子类；
- 导入成功读模型投影：publish 后四表（classes/properties/axioms/rules）行存在且与种子类目一致；
- 重复 slug 导入被拒：409（uk_ontologies_tenant_id_iri_base）且零孤儿制品、零冗余行；
- inspect 失败拒绝导入：lint 违例 / 解析失败均 4204，项目行不落库。

夹具纪律（同 test_publish_projection）：真实 PG（本地 deploy compose），不可达即跳过整用例；
制品库指向 tmp_path（M2 本地目录实现，不污染 deploy/artifacts）；Windows psycopg 要求
Selector 事件循环——导入期固定策略。API 装配（照 tests/mcp/test_a2a_http.py 的
ASGITransport 模式）：最小 FastAPI 挂 ontology 真实路由，dependency_overrides 注入测试
主体与会话工厂（提交/回滚语义同 deps.get_session），GatewayError 经平台 error_response
同构转统一错误体（替代 GlobalExceptionMiddleware）。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request
from starlette.responses import JSONResponse

from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.ontology.api import ontology as ontology_api
from services.ontology.business import seed_service
from services.ontology.data.orm import Axiom as AxiomORM
from services.ontology.data.orm import OntoClass as OntoClassORM
from services.ontology.data.orm import Ontology as OntologyORM
from services.ontology.data.orm import OntologyChangeset as OntologyChangesetORM
from services.ontology.data.orm import OntologyVersion as OntologyVersionORM
from services.ontology.data.orm import OntoProperty as OntoPropertyORM
from services.ontology.data.orm import Rule as RuleORM
from services.platform.config import Settings
from services.platform.deps import Principal, get_current_principal, get_session
from services.platform.errors import GatewayError, error_response

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

SLUG = "power-seed-it"

# 种子资产域命名空间（power_outage_seed.ttl 内容定死，与项目 iri_base 无关——投影 IRI 即此空间）
PWR = "https://ontology-agent.dev/ns/power#"
_SEED_RULE_NAMES = {
    "WorkTicketShape",
    "DispatchedTicketShape",
    "OutageEventShape",
    "FeederShape",
    "TransformerShape",
    "RepairCrewShape",
    "RestorationPlanShape",
    "MaintenanceWindowShape",
}

# 损坏资产样本：行动闭环断裂（缺 triggeredByEvent/guardedByRule → LINT_ACTION_NOT_CLOSED）
_LINT_VIOLATION_TTL = """
@prefix ob2: <https://ontology-agent.dev/ns/ob2#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix task: <https://ontology-agent.dev/ns/task#> .
@prefix bad: <http://ontology-agent.local/o/broken#> .
bad:BadAction a owl:Class ; rdfs:subClassOf ob2:Action ; task:executionMode task:deterministic .
"""

# 残缺三元组（语句无终结点）→ Turtle 解析失败
_BROKEN_TTL = "@prefix p: <http://x/> . p:Feeder a owl:Class"


@dataclass(frozen=True)
class SeedApiEnv:
    """用例环境：HTTP 客户端 + 标识 + 会话工厂 + 制品库根（tmp_path）。"""

    client: AsyncClient
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    factory: async_sessionmaker[AsyncSession]
    artifacts: Path


def _principal(tenant_id: uuid.UUID, user_id: uuid.UUID) -> Principal:
    """测试主体：ontology 全域 scope（绕过 JWT 中间件，同 tests/gateway/conftest make_principal 模式）。"""
    return Principal(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "roles": ["member"],
            "scopes": [
                "ontology:read",
                "ontology:write",
                "review:submit",
                "review:approve:ontology",
                "ontology:publish",
            ],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


async def _gateway_error_body(request: Request, exc: GatewayError) -> JSONResponse:
    """GatewayError → 统一错误体（error_response 同构；测试 app 就地注册同款转换）。"""
    return error_response(request, exc.code, exc.message, status_code=exc.status_code, detail=exc.detail)


@pytest.fixture
async def seed_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SeedApiEnv]:
    """真实 PG + 真实路由 + tmp_path 制品库；PG 不可达即跳过整用例（同 tests/ontology 夹具纪律）。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过种子导入 API 集成用例")
    await probe.dispose()

    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db, db.begin():
        tenant = TenantORM(name="seed-it-租户", slug=f"seed-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()  # uuid7 PK 在 flush 时分配，依赖行需引用真实 id
        user = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        db.add(user)
        await db.flush()  # changeset.applicant_id / version.published_by 均 FK users.id
    principal = _principal(tenant.id, user.id)
    artifacts_root = tmp_path / "artifacts"  # 制品库根独立于 tmp_path 其他内容（损坏资产样本放 assets/）

    async def _test_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:  # 提交/回滚语义同 deps.get_session
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def _repo_factory(db: AsyncSession, tenant_id: uuid.UUID):  # noqa: ANN202 — 与被替身函数同签名
        from services.ontology.data.repo_impl.ontology_repo import LocalArtifactStore, PgOntologyRepository

        return PgOntologyRepository(db, tenant_id, artifacts=LocalArtifactStore(artifacts_root))

    monkeypatch.setattr(ontology_api, "PgOntologyRepository", _repo_factory)

    app = FastAPI()
    app.include_router(ontology_api.router)
    app.add_exception_handler(GatewayError, _gateway_error_body)
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_session] = _test_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        yield SeedApiEnv(client=client, tenant_id=tenant.id, user_id=user.id, factory=factory, artifacts=artifacts_root)

    async with factory() as db, db.begin():  # FK 逆序清理（读模型四表→版本→变更单→本体→用户→租户）
        for orm in (
            RuleORM,
            AxiomORM,
            OntoPropertyORM,
            OntoClassORM,
            OntologyVersionORM,
            OntologyChangesetORM,
            OntologyORM,
        ):
            await db.execute(delete(orm).where(orm.tenant_id == tenant.id))
        await db.execute(delete(UserORM).where(UserORM.id == user.id))
        await db.execute(delete(TenantORM).where(TenantORM.id == tenant.id))
    await engine.dispose()


def _artifact_names(env: SeedApiEnv) -> list[str]:
    if not env.artifacts.exists():
        return []
    return sorted(p.relative_to(env.artifacts).as_posix() for p in env.artifacts.rglob("*.ttl"))


async def test_种子导入成功_项目版本制品三落位(seed_api: SeedApiEnv) -> None:
    # Act
    resp = await seed_api.client.post(
        "/ontologies/import-seed", json={"slug": SLUG, "display_name": "电力停电种子本体"}
    )
    # Assert：201 + 项目与版本摘要（published/head=v1 + 自检报告）
    assert resp.status_code == 201, resp.text
    body = resp.json()
    ontology, version = body["ontology"], body["version"]
    assert ontology["name"] == "电力停电种子本体"
    assert ontology["status"] == "published"
    assert ontology["iri_base"].endswith(f"/{SLUG}#")
    assert version["version"] == "v1" and version == ontology["head_version"]
    assert body["seed_report"]["lint_ok"] is True
    assert body["seed_report"]["class_count"] >= 20  # 种子规模口径 20~50 类（锚点 §7）
    assert body["seed_report"]["action_count"] == 5 and body["seed_report"]["shape_count"] >= 7

    # Assert：制品内容含种子类且 checksum 与种子资产一致（三方巡检锚点）
    seed_text = seed_service.SEED_PATH.read_text(encoding="utf-8")
    assert _artifact_names(seed_api) == [version["artifact_key"]]
    artifact = (seed_api.artifacts / version["artifact_key"]).read_text(encoding="utf-8")
    assert "Feeder" in artifact and "DispatchRepair" in artifact  # 对象层与行动层种子类在制品
    assert version["checksum"] == hashlib.sha256(seed_text.encode("utf-8")).hexdigest()

    # Assert：PG 持久化——聚合 head 指向 v1、变更单 published、版本行恰一
    async with seed_api.factory() as db:
        row = await db.get(OntologyORM, uuid.UUID(ontology["id"]))
        assert row is not None and row.status == "published" and row.current_version_id is not None
        changesets = (
            (await db.execute(select(OntologyChangesetORM).where(OntologyChangesetORM.tenant_id == seed_api.tenant_id)))
            .scalars()
            .all()
        )
        versions = (
            (await db.execute(select(OntologyVersionORM).where(OntologyVersionORM.tenant_id == seed_api.tenant_id)))
            .scalars()
            .all()
        )
    assert len(changesets) == 1 and changesets[0].status == "published"
    assert changesets[0].applicant_id == seed_api.user_id  # solo 档：导入人即审批人（留痕可追溯）
    assert len(versions) == 1 and versions[0].version == "v1"
    assert versions[0].checksum == version["checksum"]


async def test_种子导入成功_读模型四表投影落位(seed_api: SeedApiEnv) -> None:
    """导入成功即读模型就绪（2026-09-28 补投影链）：publish 后四表行存在且内容与种子类目一致。

    链路同 L2 publish 路由同款：import_seed_as_project 在聚合 publish 后显式调
    project_published_version（routes 缺省=制品图重跑 lint 取权威路由，rollback 同款），
    投影与版本行同会话事务落库——无需任何后续调用，检索/工作台即可消费。
    """
    # Act
    resp = await seed_api.client.post("/ontologies/import-seed", json={"slug": SLUG})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    seed_report = body["seed_report"]
    # Assert —— 四表行存在：类数与种子自检口径一致（同一 lint/投影产出），其余三表非空
    async with seed_api.factory() as db:
        classes = (
            (await db.execute(select(OntoClassORM).where(OntoClassORM.tenant_id == seed_api.tenant_id)))
            .scalars()
            .all()
        )
        properties = (
            (await db.execute(select(OntoPropertyORM).where(OntoPropertyORM.tenant_id == seed_api.tenant_id)))
            .scalars()
            .all()
        )
        axioms = (
            (await db.execute(select(AxiomORM).where(AxiomORM.tenant_id == seed_api.tenant_id))).scalars().all()
        )
        rules = ((await db.execute(select(RuleORM).where(RuleORM.tenant_id == seed_api.tenant_id))).scalars().all())
        changeset_row = (
            (
                await db.execute(
                    select(OntologyChangesetORM).where(OntologyChangesetORM.tenant_id == seed_api.tenant_id)
                )
            )
            .scalars()
            .one()
        )
        version_row = (
            (
                await db.execute(
                    select(OntologyVersionORM).where(OntologyVersionORM.tenant_id == seed_api.tenant_id)
                )
            )
            .scalars()
            .one()
        )
    assert len(classes) == seed_report["class_count"] >= 20  # 种子规模口径 20~50 类（锚点 §7）
    assert len(properties) >= 1 and len(axioms) >= 1 and len(rules) >= 1

    # Assert —— classes 与种子对象/行动层一致：标签、层级、OB2 行动类判定
    by_iri = {c.iri: c for c in classes}
    feeder = by_iri[f"{PWR}Feeder"]
    assert feeder.name == "Feeder" and feeder.label == "10kV 馈线" and feeder.is_behavior is False
    assert by_iri[f"{PWR}ScheduledOutage"].subclass_of == [f"{PWR}OutageEvent"]  # 种子 subClassOf 层级入列
    assert by_iri[f"{PWR}DispatchRepair"].is_behavior is True  # 行动类（subClassOf ob2:Action 闭包）
    assert all(c.version_id == version_row.id for c in classes)  # 挂靠 v1 版本行
    assert all(c.changeset_id == changeset_row.id for c in classes)  # 来源变更单留痕

    # Assert —— properties 与种子属性层一致：域/值域 + shape 约束随属性投影
    prop_by_iri = {p.iri: p for p in properties}
    supplies = prop_by_iri[f"{PWR}supplies"]
    assert supplies.kind == "object"
    assert supplies.domain_iri == f"{PWR}Feeder" and supplies.range_iri == f"{PWR}Transformer"
    assert prop_by_iri[f"{PWR}feederId"].kind == "datatype"
    assert prop_by_iri[f"{PWR}feederId"].constraints["pattern"] == "^F-[0-9]{3}$"  # FeederShape 约束投影

    # Assert —— axioms 含种子 subClassOf 直接公理（行动闭环与事件层级可追溯）
    axiom_triples = {(a.kind, a.subject_iri, a.object_iri) for a in axioms}
    assert ("subClassOf", f"{PWR}FaultOutage", f"{PWR}OutageEvent") in axiom_triples
    assert ("subClassOf", f"{PWR}DispatchRepair", "https://ontology-agent.dev/ns/ob2#Action") in axiom_triples

    # Assert —— rules 与种子 8 条 ob2:Rule 一致：三路由判定落列（权威=lint §2.3）
    rule_by_name = {r.name: r for r in rules}
    assert set(rule_by_name) == _SEED_RULE_NAMES
    assert rule_by_name["RepairCrewShape"].route == "shacl"  # 纯 R2 算子封闭集通道
    assert rule_by_name["DispatchedTicketShape"].route == "engine"  # 含 sh:sparql 超 R2 算子集 → R3
    assert all(r.version_id == version_row.id and r.changeset_id == changeset_row.id for r in rules)


async def test_重复slug导入被拒_409且零孤儿制品(seed_api: SeedApiEnv) -> None:
    # Arrange —— 首次导入成功（display_name 缺省 → 种子定名）
    first = await seed_api.client.post("/ontologies/import-seed", json={"slug": SLUG})
    assert first.status_code == 201, first.text
    assert first.json()["ontology"]["name"] == seed_service.SEED_PROJECT_NAME
    artifacts_before = _artifact_names(seed_api)
    # Act —— 同 slug 二次导入
    second = await seed_api.client.post("/ontologies/import-seed", json={"slug": SLUG})
    # Assert：409 统一错误体（uk_ontologies_tenant_id_iri_base 兜底），无新增制品与冗余行
    assert second.status_code == 409, second.text
    assert second.json()["code"] == 409
    assert _artifact_names(seed_api) == artifacts_before  # 零孤儿制品
    async with seed_api.factory() as db:
        ontologies = (
            await db.execute(
                select(func.count()).select_from(OntologyORM).where(OntologyORM.tenant_id == seed_api.tenant_id)
            )
        ).scalar_one()
        versions = (
            await db.execute(
                select(func.count())
                .select_from(OntologyVersionORM)
                .where(OntologyVersionORM.tenant_id == seed_api.tenant_id)
            )
        ).scalar_one()
    assert ontologies == 1 and versions == 1


@pytest.mark.parametrize("kind", ["lint", "parse"], ids=["lint违例", "解析失败"])
async def test_inspect失败拒绝导入_项目不落库(
    seed_api: SeedApiEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # Arrange —— SEED_PATH 指向损坏资产（业务函数调用期读取模块全局，monkeypatch 生效；
    # 放 assets/ 子目录避免与制品库根 tmp_path 撞车）
    broken = tmp_path / "assets" / "broken_seed.ttl"
    broken.parent.mkdir(exist_ok=True)
    broken.write_text(_LINT_VIOLATION_TTL if kind == "lint" else _BROKEN_TTL, encoding="utf-8")
    monkeypatch.setattr(seed_service, "SEED_PATH", broken)
    # Act
    resp = await seed_api.client.post("/ontologies/import-seed", json={"slug": "broken-seed"})
    # Assert：4204 拒绝导入（HTTP 409 平台错误体），项目行零落库、零制品
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == 4204
    assert "拒绝导入" in body["message"]
    async with seed_api.factory() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(OntologyORM).where(OntologyORM.tenant_id == seed_api.tenant_id)
            )
        ).scalar_one()
    assert count == 0
    assert _artifact_names(seed_api) == []
