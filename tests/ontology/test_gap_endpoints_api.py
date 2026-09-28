# tests/ontology/test_gap_endpoints_api.py
"""ontology 域缺口端点 API 用例（api/01 §5.3 已注册未实现批：PUT / search / diff / query / reason）。

覆盖：
- PUT /ontologies/{id}：部分更新语义（仅显式字段生效、null 清空 description）+ 防呆（空对象/未知 id/非空字段 null）；
- POST /ontologies/search：名称/描述/命名空间 IRI 片段匹配 + limit + 零命中（M2 最小闭环口径，偏离登记）；
- GET /ontologies/{id}/diff：两版本读模型差异（类增/类改字段级/公理增 + summary）+ 防呆
  （未发布 409、未知版本 404、单版本无 base 409、同版本全零差异）；
- POST /ontologies/{id}/query：SELECT/ASK 只读面 + 注入防护（INSERT/DELETE/CONSTRUCT/语法错误均 422/3001
  且零副作用）+ 未发布/未知本体防呆；
- POST /ontologies/{id}/reason：consistency（gate.v1 门禁级）/ classification|entailment（owl2_rl 闭包）/
  semantic 显式拒绝（LLM 不入确定性推理面，宪法 2/3）。

夹具纪律（同 test_seed_import_api）：真实 PG（本地 deploy compose），不可达即跳过整用例；
制品库指向 tmp_path；Windows psycopg 要求 Selector 事件循环；ASGITransport 最小 app 挂真实路由。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS
from sqlalchemy import delete
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

SLUG = "power-gap-it"
PWR = "https://ontology-agent.dev/ns/power#"  # 种子资产域命名空间（与项目 iri_base 无关）


@dataclass(frozen=True)
class GapApiEnv:
    """用例环境：HTTP 客户端 + 标识 + 会话工厂 + 制品库根（tmp_path）。"""

    client: AsyncClient
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    factory: async_sessionmaker[AsyncSession]
    artifacts: Path


def _principal(tenant_id: uuid.UUID, user_id: uuid.UUID) -> Principal:
    """测试主体：ontology 全域 scope（同 test_seed_import_api._principal 模式）。"""
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
    return error_response(request, exc.code, exc.message, status_code=exc.status_code, detail=exc.detail)


class _FakeTierReader:
    """治理档位读取器替身（08 §2.4 duck-typing 消费面：solo 档种子默认，approve 五动词需要）。"""

    async def tier(self, tenant_id: uuid.UUID):
        from services.review.domain.approval_chain import GovernanceTier

        return GovernanceTier.SOLO


@pytest.fixture
async def gap_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[GapApiEnv]:
    """真实 PG + 真实路由 + tmp_path 制品库；PG 不可达即跳过整用例（同 tests/ontology 夹具纪律）。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 ontology 缺口端点 API 用例")
    await probe.dispose()

    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db, db.begin():
        tenant = TenantORM(name="gap-it-租户", slug=f"gap-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        user = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        db.add(user)
        await db.flush()
    principal = _principal(tenant.id, user.id)
    artifacts_root = tmp_path / "artifacts"

    async def _test_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
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
    app.state.review_approvals = _FakeTierReader()  # approve 五动词档位读取面（gateway lifespan 装配位）

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        yield GapApiEnv(client=client, tenant_id=tenant.id, user_id=user.id, factory=factory, artifacts=artifacts_root)

    async with factory() as db, db.begin():  # FK 逆序清理（同 test_seed_import_api）
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


# ---- 装配辅助


def _v2_turtle(seed: str) -> str:
    """v2 制品 = 种子 + 类层级深化（三级链）+ 实例 + 新类 + label 修订（gate 全绿前提下的差异源）。"""
    graph = Graph()
    graph.parse(data=seed, format="turtle")
    pwr = Namespace(PWR)
    graph.remove((pwr.Feeder, RDFS.label, None))
    graph.add((pwr.Feeder, RDFS.label, Literal("10kV 馈线（v2 修订）", lang="zh")))
    graph.add((pwr.PlannedMaintenance, RDF.type, OWL.Class))
    graph.add((pwr.PlannedMaintenance, RDFS.subClassOf, pwr.ScheduledOutage))
    graph.add((pwr.PlannedMaintenance, RDFS.label, Literal("计划检修停运", lang="zh")))
    graph.add((pwr.NewSensor, RDF.type, OWL.Class))
    graph.add((pwr.NewSensor, RDFS.label, Literal("新增传感类", lang="zh")))
    graph.add((pwr.outageDemo, RDF.type, pwr.PlannedMaintenance))
    return graph.serialize(format="turtle")


async def _publish_next_version(env: GapApiEnv, ontology_id: str) -> str:
    """变更单五动词链发布下一版本（v1 之上），返回新版本号。"""
    seed = seed_service.SEED_PATH.read_text(encoding="utf-8")
    turtle = _v2_turtle(seed)
    resp = await env.client.post(f"/ontologies/{ontology_id}/changesets", json={"title": "缺口批 v2 修订"})
    assert resp.status_code == 201, resp.text
    cid = resp.json()["id"]
    resp = await env.client.post(
        f"/ontologies/{ontology_id}/changesets/{cid}/submit", json={"gate_ok": False, "turtle": turtle}
    )
    assert resp.status_code == 202, resp.text
    resp = await env.client.post(f"/ontologies/{ontology_id}/changesets/{cid}/approve", json={"note": "缺口批自审"})
    assert resp.status_code == 202, resp.text
    resp = await env.client.post(
        f"/ontologies/{ontology_id}/changesets/{cid}/publish", json={"gate_ok": True, "turtle": turtle}
    )
    assert resp.status_code == 202, resp.text
    head = resp.json()["head_version"]
    assert head is not None
    return head["version"]


async def _import_seed(env: GapApiEnv) -> str:
    """种子导入（v1 发布态），返回本体 id。"""
    resp = await env.client.post("/ontologies/import-seed", json={"slug": SLUG})
    assert resp.status_code == 201, resp.text
    return resp.json()["ontology"]["id"]


# ---- PUT /ontologies/{id}


async def test_PUT元信息更新_部分字段生效并落库(gap_api: GapApiEnv) -> None:
    # Arrange：draft 本体（含 description 初始值）
    created = await gap_api.client.post(
        "/ontologies", json={"name": "配网分析本体", "slug": "put-it", "description": "初始描述"}
    )
    assert created.status_code == 201, created.text
    ontology_id = created.json()["id"]
    # Act：部分更新（只带 name 与 scheme_tier）
    resp = await gap_api.client.put(
        f"/ontologies/{ontology_id}", json={"name": "配网故障分析本体", "scheme_tier": "heavy"}
    )
    # Assert：200 + name/scheme_tier 生效、description 保持不变
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "配网故障分析本体"
    assert body["scheme_tier"] == "heavy"
    assert body["description"] == "初始描述"
    async with gap_api.factory() as db:  # PG 持久化
        row = await db.get(OntologyORM, uuid.UUID(ontology_id))
    assert row is not None
    assert (row.name, row.scheme_tier, row.description) == ("配网故障分析本体", "heavy", "初始描述")


async def test_PUT显式null清空description(gap_api: GapApiEnv) -> None:
    created = await gap_api.client.post("/ontologies", json={"name": "清空面本体", "description": "将被清空"})
    ontology_id = created.json()["id"]
    resp = await gap_api.client.put(f"/ontologies/{ontology_id}", json={"description": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] is None  # 显式 null=清空（部分更新语义，docstring 注明）


async def test_PUT防呆_空对象与未知本体与非空字段null(gap_api: GapApiEnv) -> None:
    created = await gap_api.client.post("/ontologies", json={"name": "防呆面本体"})
    ontology_id = created.json()["id"]
    # 空对象体：未显式提供任何字段 → 422/3001
    resp = await gap_api.client.put(f"/ontologies/{ontology_id}", json={})
    assert resp.status_code == 422 and resp.json()["code"] == 3001, resp.text
    # 非空字段显式 null → 422/3001（清空语义仅适用于 description）
    resp = await gap_api.client.put(f"/ontologies/{ontology_id}", json={"name": None})
    assert resp.status_code == 422 and resp.json()["code"] == 3001, resp.text
    # 未知本体 → 404
    resp = await gap_api.client.put(f"/ontologies/{uuid.uuid4()}", json={"name": "不存在"})
    assert resp.status_code == 404, resp.text


# ---- POST /ontologies/search


async def test_SEARCH_名称与命名空间片段命中(gap_api: GapApiEnv) -> None:
    await gap_api.client.post(
        "/ontologies", json={"name": "电力停电分析本体", "slug": "outage-a", "description": "配网域"}
    )
    await gap_api.client.post("/ontologies", json={"name": "配变台账本体", "slug": "transformer-b"})
    # 名称片段命中
    resp = await gap_api.client.post("/ontologies/search", json={"query": "停电"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1 and body["items"][0]["name"] == "电力停电分析本体"
    # 命名空间 IRI 片段命中（iri_base 含 slug）
    resp = await gap_api.client.post("/ontologies/search", json={"query": "transformer-b"})
    body = resp.json()
    assert body["total"] == 1 and body["items"][0]["iri_base"].endswith("/transformer-b#")
    # 描述片段命中
    resp = await gap_api.client.post("/ontologies/search", json={"query": "配网域"})
    assert resp.json()["total"] == 1
    # 零命中
    resp = await gap_api.client.post("/ontologies/search", json={"query": "不存在的词xyz"})
    assert resp.status_code == 200 and resp.json() == {"items": [], "total": 0, "limit": 20}
    # limit 生效
    resp = await gap_api.client.post("/ontologies/search", json={"query": "本体", "limit": 1})
    assert resp.json()["total"] == 1 and resp.json()["limit"] == 1


async def test_SEARCH_空查询被拒(gap_api: GapApiEnv) -> None:
    resp = await gap_api.client.post("/ontologies/search", json={"query": ""})
    assert resp.status_code == 422, resp.text  # DTO min_length=1（片段语义要求非空）


# ---- GET /ontologies/{id}/diff


async def test_DIFF_两版本读模型差异_增删改清单(gap_api: GapApiEnv) -> None:
    ontology_id = await _import_seed(gap_api)
    await _publish_next_version(gap_api, ontology_id)
    # Act：缺省 base/target（base=v1 前一版本，target=v2 head）
    resp = await gap_api.client.get(f"/ontologies/{ontology_id}/diff")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["base_version"], body["target_version"]) == ("v1", "v2")
    # 类：新增 NewSensor；修改 Feeder（label 字段级 before/after）
    added_keys = [e["key"] for e in body["classes"]["added"]]
    assert f"{PWR}NewSensor" in added_keys and f"{PWR}PlannedMaintenance" in added_keys
    modified = {e["key"]: e["changes"] for e in body["classes"]["modified"]}
    assert f"{PWR}Feeder" in modified
    label_change = next(c for c in modified[f"{PWR}Feeder"] if c["field"] == "label")
    assert label_change["after"] == "10kV 馈线（v2 修订）"
    # 公理：新增 PlannedMaintenance subClassOf ScheduledOutage
    assert any(f"{PWR}PlannedMaintenance" in e["key"] and "subClassOf" in e["key"] for e in body["axioms"]["added"])
    # summary 扁平计数与分组一致
    assert body["summary"]["classes_added"] == len(body["classes"]["added"]) >= 2
    assert body["summary"]["classes_modified"] == len(body["classes"]["modified"]) >= 1
    # 显式 base/target：同版本对比 → 全零差异
    resp = await gap_api.client.get(f"/ontologies/{ontology_id}/diff?base=v2&target=v2")
    body = resp.json()
    assert body["summary"]["classes_modified"] == 0 and body["classes"]["unchanged"] > 0


async def test_DIFF_防呆_未发布未知本体未知版本单版本无base(gap_api: GapApiEnv) -> None:
    # 未发布（draft 无版本）→ 409/4201
    created = await gap_api.client.post("/ontologies", json={"name": "未发布本体", "slug": "diff-draft"})
    ontology_id = created.json()["id"]
    resp = await gap_api.client.get(f"/ontologies/{ontology_id}/diff")
    assert resp.status_code == 409 and resp.json()["code"] == 4201, resp.text
    # 未知本体 → 404
    resp = await gap_api.client.get(f"/ontologies/{uuid.uuid4()}/diff")
    assert resp.status_code == 404, resp.text
    # 已发布：未知版本名 → 404；单版本缺省 base → 409
    ontology_id = await _import_seed(gap_api)
    resp = await gap_api.client.get(f"/ontologies/{ontology_id}/diff?base=v9&target=v1")
    assert resp.status_code == 404 and resp.json()["code"] == 4201, resp.text
    resp = await gap_api.client.get(f"/ontologies/{ontology_id}/diff")
    assert resp.status_code == 409 and resp.json()["code"] == 4201, resp.text


# ---- POST /ontologies/{id}/query


async def test_QUERY_select与ask只读面(gap_api: GapApiEnv) -> None:
    ontology_id = await _import_seed(gap_api)
    # SELECT：种子类清单（种子规模口径 ≥20 类）
    sparql = (
        "PREFIX owl: <http://www.w3.org/2002/07/owl#>\n"
        f"PREFIX pwr: <{PWR}>\n"
        "SELECT ?c ?label WHERE { ?c a owl:Class ; rdfs:label ?label . }"
    )
    resp = await gap_api.client.post(f"/ontologies/{ontology_id}/query", json={"sparql": sparql})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["form"] == "select" and body["variables"] == ["c", "label"]
    assert len(body["rows"]) >= 20 and not body["truncated"]
    assert any(row["c"] == f"{PWR}Feeder" and "馈线" in (row["label"] or "") for row in body["rows"])
    assert body["elapsed_ms"] >= 0
    # ASK：布尔面
    sparql = f"PREFIX owl: <http://www.w3.org/2002/07/owl#>\nASK {{ ?c a owl:Class . FILTER(STR(?c) = '{PWR}Feeder') }}"
    resp = await gap_api.client.post(f"/ontologies/{ontology_id}/query", json={"sparql": sparql})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["form"] == "ask" and body["boolean"] is True and body["rows"] == []


@pytest.mark.parametrize(
    "sparql",
    [
        "INSERT DATA { <http://x/a> <http://x/b> <http://x/c> }",
        "DELETE WHERE { ?s ?p ?o }",
        "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
        "DESCRIBE ?s",
        "SELECT ?s WHERE",
    ],
    ids=["insert", "delete", "construct", "describe", "语法错误"],
)
async def test_QUERY_注入防护_非法语句422且零副作用(gap_api: GapApiEnv, sparql: str) -> None:
    ontology_id = await _import_seed(gap_api)
    detail = await gap_api.client.get(f"/ontologies/{ontology_id}")
    checksum_before = detail.json()["head_version"]["checksum"]
    # Act
    resp = await gap_api.client.post(f"/ontologies/{ontology_id}/query", json={"sparql": sparql})
    # Assert：422/3001（执行前白名单拒绝），制品 checksum 不变（零副作用）
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == 3001
    detail = await gap_api.client.get(f"/ontologies/{ontology_id}")
    assert detail.json()["head_version"]["checksum"] == checksum_before


async def test_QUERY_防呆_未发布未知本体与预算下界(gap_api: GapApiEnv) -> None:
    created = await gap_api.client.post("/ontologies", json={"name": "未发布查询本体", "slug": "query-draft"})
    ontology_id = created.json()["id"]
    resp = await gap_api.client.post(f"/ontologies/{ontology_id}/query", json={"sparql": "ASK { ?s ?p ?o }"})
    assert resp.status_code == 409 and resp.json()["code"] == 4201, resp.text
    resp = await gap_api.client.post(f"/ontologies/{uuid.uuid4()}/query", json={"sparql": "ASK { ?s ?p ?o }"})
    assert resp.status_code == 404, resp.text
    # 预算下界（timeout_ms < 100）→ DTO 校验 422（最小测试 app 无统一错误体转换，仅断状态码）
    ontology_id = await _import_seed(gap_api)
    resp = await gap_api.client.post(
        f"/ontologies/{ontology_id}/query", json={"sparql": "ASK { ?s ?p ?o }", "timeout_ms": 50}
    )
    assert resp.status_code == 422, resp.text


# ---- POST /ontologies/{id}/reason


async def test_REASON_consistency_门禁级一致(gap_api: GapApiEnv) -> None:
    ontology_id = await _import_seed(gap_api)
    resp = await gap_api.client.post(f"/ontologies/{ontology_id}/reason", json={"type": "consistency"})
    assert resp.status_code == 202, resp.text  # 契约成功码 202（api/01 §5.3 reason 行）
    body = resp.json()
    assert body["type"] == "consistency" and body["engine"] == "gate.v1"
    assert body["conforms"] is True and body["lint_ok"] is True and body["shacl_conforms"] is True
    assert body["violations"] == [] and body["elapsed_ms"] >= 0


async def test_REASON_闭包_分类与推导(gap_api: GapApiEnv) -> None:
    ontology_id = await _import_seed(gap_api)
    await _publish_next_version(gap_api, ontology_id)  # v2：三级类链 + 实例（分类闭包的差异源）
    # classification：subClassOf 传递 + 实例归类
    resp = await gap_api.client.post(f"/ontologies/{ontology_id}/reason", json={"type": "classification"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["type"] == "classification" and body["engine"] == "owl2_rl" and body["conforms"] is True
    assert body["conclusion_count"] >= 2 and body["elapsed_ms"] >= 0
    conclusions = {(c["subject"], c["predicate"], c["object"]) for c in body["conclusions"]}
    assert (f"{PWR}PlannedMaintenance", f"{RDFS}subClassOf", f"{PWR}OutageEvent") in conclusions  # 传递闭包
    assert (f"{PWR}outageDemo", f"{RDF}type", f"{PWR}OutageEvent") in conclusions  # 实例归类
    # entailment：全谓词闭包 ⊇ 分类子集
    resp = await gap_api.client.post(f"/ontologies/{ontology_id}/reason", json={"type": "entailment"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["conclusion_count"] >= 2 and sum(body["counts"].values()) == body["conclusion_count"]


@pytest.mark.parametrize(
    ("rtype", "needle"),
    [("semantic", "候选审核链"), ("telepathy", "consistency")],
    ids=["semantic_LLM面拒绝", "未知type"],
)
async def test_REASON_防呆_非法type与未发布与未知本体(gap_api: GapApiEnv, rtype: str, needle: str) -> None:
    ontology_id = await _import_seed(gap_api)
    resp = await gap_api.client.post(f"/ontologies/{ontology_id}/reason", json={"type": rtype})
    assert resp.status_code == 422 and resp.json()["code"] == 3001, resp.text
    assert needle in resp.json()["message"]
    # 未发布 → 409/4201；未知本体 → 404
    created = await gap_api.client.post("/ontologies", json={"name": "未发布推理本体", "slug": "reason-draft"})
    resp = await gap_api.client.post(f"/ontologies/{created.json()['id']}/reason", json={"type": "consistency"})
    assert resp.status_code == 409 and resp.json()["code"] == 4201, resp.text
    resp = await gap_api.client.post(f"/ontologies/{uuid.uuid4()}/reason", json={"type": "consistency"})
    assert resp.status_code == 404, resp.text
