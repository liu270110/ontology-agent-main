"""独立进程级 e2e（M4 出口销项：外部 Agent 经 MCP 调通平台检索——评审-2026-09-27 P1-1 运行层证据）。

与测试层 in-memory Client（test_server_tools.py）的本质差异：本文件起**真实子进程**
``python -m services.mcp --transport http``（共享工厂 bootstrap 全量装配，与 gateway 同一面），
fastmcp Client 经 Streamable HTTP 真连，覆盖 13 篇 §4 出口「外部 Claude 调通平台检索」：

① tools/list：七 tool + memory.invalidate + writeback.status 全部在场；
② 实调 knowledge.search（PG 种子数据 + 共享工厂同款装配）→ 返回 citations（引用）；
③ 实调 action.invoke（Mock 电力工单连接器）→ 受理凭证；幂等重放同 ledger/同受理号；
   writeback.status 按凭证兑现状态（api/03 §3.9）；
④ 未授权 scope（memory:write 未授予）→ 2001（PDP deny-by-default 红线在运行层成立）。

环境纪律：integration 标记；fastmcp 缺失 skip；本地 PG 不可达 skip。
Windows：子进程以 TerminateProcess 硬终止（先例=tests/integration/test_pipeline_crash_resume.py）；
psycopg 异步要求 Selector 事件循环（导入期固定策略，同 tests/writeback 纪律）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

fastmcp = pytest.importorskip("fastmcp")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # psycopg 异步要求（Windows）

from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from services.iam.data.orm import AuditLog as AuditLogORM  # noqa: E402
from services.iam.data.orm import Tenant as TenantORM  # noqa: E402
from services.kb.data.orm import Document as DocumentORM  # noqa: E402
from services.kb.data.orm import DocumentChunk as DocumentChunkORM  # noqa: E402
from services.kb.data.orm import KbCollection as KbCollectionORM  # noqa: E402
from services.mcp.server import SEVEN_TOOLS  # noqa: E402
from services.platform.config import Settings  # noqa: E402
from services.platform.db import registry as orm_registry  # noqa: E402, F401  # 全模块 ORM 入 metadata（FK 解析）
from services.writeback.adapters.mock_power_ticket import ACTION_IRI_CREATE_ORDER  # noqa: E402
from services.writeback.data.orm import WritebackLedgerORM  # noqa: E402

pytestmark = [pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[2]
READY_TIMEOUT_S = 240.0  # 子进程冷启动（rdflib/pyshacl/fastmcp/uvicorn 导入链），慢机兜底
# 匿名授权集：覆盖 ①②③ 所需 scope；刻意不含 memory:write（供断言④ 2001）
GRANTED_SCOPES = "kb:read,ontology:read,memory:read,action:invoke"
# BM25 'simple' 配置按空格分词（中文分词随 M3+，tests/kb/test_citation_baseline.py 同口径）
SEED_CONTENT = "馈线 F009 故障隔离流程 保护动作后 调度员 执行 故障隔离 并 记录 保护动作 情况"
SEED_QUERY = "馈线 F009 故障隔离"


@pytest.fixture
async def pg_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """本地 PG 会话工厂；不可达即跳过（integration 纪律）。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过独立进程 e2e")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def seeded_kb(pg_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """独立租户 + 集合 + 单文档单 chunk（空格分词适配 BM25 'simple'；直插绕过流水线，确定性）。"""
    async with pg_factory() as db, db.begin():
        tenant = TenantORM(name="mcp-e2e-租户", slug=f"mcp-e2e-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="mcp-e2e-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        doc = DocumentORM(
            tenant_id=tenant.id,
            kb_collection_id=collection.id,
            title="馈线故障处置手册（e2e）",
            source_type="upload",
            size_bytes=len(SEED_CONTENT.encode()),
            minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
            checksum_sha256=hashlib.sha256(SEED_CONTENT.encode()).hexdigest(),
            meta={"content": SEED_CONTENT},
            status="indexed",
        )
        db.add(doc)
        await db.flush()
        db.add(
            DocumentChunkORM(
                tenant_id=tenant.id,
                document_id=doc.id,
                seq=0,
                content=SEED_CONTENT,
                token_count=len(SEED_CONTENT) // 2,
                meta={"span": [0, len(SEED_CONTENT)]},
            )
        )
    env = {"tenant_id": tenant.id, "collection_id": collection.id, "document_id": doc.id}
    yield env
    # 清理（audit_logs / writeback_ledger 无 FK 但按租户收口；kb 链按 FK 逆序）
    async with pg_factory() as db, db.begin():
        for stmt in (
            delete(AuditLogORM).where(AuditLogORM.tenant_id == env["tenant_id"]),
            delete(WritebackLedgerORM).where(WritebackLedgerORM.tenant_id == env["tenant_id"]),
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.id == env["document_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _spawn_server(port: int, tenant_id: uuid.UUID) -> tuple[subprocess.Popen, Path]:
    """起独立进程（共享工厂装配）；返回 (进程, 日志文件)——提前退出由调用方带出日志。"""
    log_file = Path(tempfile.gettempdir()) / f"mcp-e2e-{port}.log"
    handle = log_file.open("w", encoding="utf-8")
    proc = subprocess.Popen(  # noqa: S603 ——测试受控参数，非 shell
        [
            sys.executable,
            "-m",
            "services.mcp",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--tenant-id",
            str(tenant_id),
            "--anonymous-scopes",
            GRANTED_SCOPES,
            "--no-external",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    handle.close()  # 子进程已持有自身副本；父进程句柄及时释放（Windows 日志文件删除不被阻塞）
    return proc, log_file


@pytest.mark.integration
async def test_独立进程e2e_工具清单_检索引用_回写受理幂等_越权拒绝(
    pg_factory: async_sessionmaker[AsyncSession], seeded_kb: dict
) -> None:
    tenant_id = seeded_kb["tenant_id"]
    port = _free_port()
    proc, log_file = await _spawn_server(port, tenant_id)
    try:
        # ---- 就绪轮询：TCP 可连即就绪；提前退出=场景失败，带出日志定位 ----
        deadline = time.monotonic() + READY_TIMEOUT_S
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log_tail = log_file.read_text(encoding="utf-8")[-2000:]
                pytest.fail(f"独立进程提前退出（exit={proc.returncode}）: {log_tail}")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    ready = True
                    break
            await asyncio.sleep(0.25)
        if not ready:
            pytest.fail(f"独立进程就绪超时（{READY_TIMEOUT_S}s）: {log_file.read_text(encoding='utf-8')[-2000:]}")

        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            # ---- ① tools/list：七 tool + 两补充项全部在场（共享工厂全量装配的运行层证据）----
            names = {t.name for t in await client.list_tools()}
            for name in (*SEVEN_TOOLS, "memory.invalidate", "writeback.status"):
                assert name in names, f"{name} 未挂载"

            # ---- ② 实调 knowledge.search：真实检索服务（PG 数据）→ citations（外部 Agent 调通平台检索）----
            search = await client.call_tool("knowledge.search", {"query": SEED_QUERY, "top_k": 5})
            payload = search.structured_content or {}
            assert payload.get("citations"), f"knowledge.search 无引用返回: {payload}"
            assert any("馈线" in (c.get("quote") or "") for c in payload["citations"])

            # ---- ③ 实调 action.invoke：受理即凭证；幂等重放同结果；writeback.status 兑现状态 ----
            instance_id = uuid.uuid4()
            invoke_args = {
                "action_iri": ACTION_IRI_CREATE_ORDER,
                "params": {"feeder": "F009", "order_no": "E2E-1", "action_instance_id": str(instance_id)},
            }
            first = (await client.call_tool("action.invoke", invoke_args)).structured_content or {}
            assert first.get("status") == "accepted", f"回写未受理: {first}"
            assert first.get("receipt", {}).get("receipt_no")
            replay = (await client.call_tool("action.invoke", dict(invoke_args))).structured_content or {}
            assert replay["ledger_id"] == first["ledger_id"]  # 同键重复请求返回原结果（api/03 §7）
            assert replay["receipt"]["receipt_no"] == first["receipt"]["receipt_no"]

            status = (
                await client.call_tool("writeback.status", {"ledger_id": first["ledger_id"]})
            ).structured_content or {}
            assert status["status"] == "accepted"  # api/03 §3.9：凭证兑现通道
            assert status["updated_at"] is not None

            # ---- ④ 未授权 scope：memory.write 未授予 → 2001（deny-by-default 在运行层成立）----
            with pytest.raises(ToolError) as exc_info:
                await client.call_tool("memory.write", {"level": "user", "content": "e2e 越权探测"})
            wire = json.loads(str(exc_info.value))
            assert wire["code"] == 2001
            assert wire["detail"]["required"] == ["memory:write"]

        # ---- 审计持久留痕（api/03 §6：独立进程 PG 汇绑定后成立）----
        async with pg_factory() as db:
            rows = (await db.execute(select(AuditLogORM).where(AuditLogORM.tenant_id == tenant_id))).scalars().all()
        audited_tools = {row.action for row in rows}
        assert "mcp.knowledge.search" in audited_tools
        assert "mcp.action.invoke" in audited_tools
        assert all(row.trace_id for row in rows)  # trace_id 贯穿
    finally:
        # Windows：terminate=TerminateProcess（硬杀，无清理机会）；POSIX=SIGTERM——先例同 crash 用例
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=30)
        log_file.unlink(missing_ok=True)
