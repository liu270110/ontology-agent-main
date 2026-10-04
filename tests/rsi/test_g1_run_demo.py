# tests/rsi/test_g1_run_demo.py
"""run_g1 demo 模式端到端用例（subprocess 真跑 services/tools/orsi/run_g1.py --demo）。

断言目标：退出码 0；两簇分别命中 L1（组合/O5）与 L2（市场/O1）；demo 标注在场（合成数据
非真实信号）；--no-demo-market 时跨域簇如实降级（未命中 + L3 skipped）；报告含生命周期
红线（status 保持 draft / plugin 只读 / L3 零幻觉上架）。
live 装配面：_open_ledger_repo 必须返回**零参可重复调用的上下文工厂**（LedgerFailureSink
drain 契约）——ocr 评审 ① 回归锚点。
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "services" / "tools" / "orsi" / "run_g1.py"), *args],
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=env,
    )


def test_run_g1_demo端到端_双簇三级起草(tmp_path: Path) -> None:
    store_dir = tmp_path / "store"
    report_path = tmp_path / "report.md"
    proc = _run(
        ["--demo", "--store-dir", str(store_dir), "--report", str(report_path)]
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "[demo] 注入合成缺口事件 12 条" in proc.stdout  # ⚠ 标注：非真实信号
    assert "种子域 unbound_action + 跨域 execution_failure" in proc.stdout

    report = report_path.read_text(encoding="utf-8")
    assert "# ORSI G1 起草运行报告" in report
    assert "demo（⚠ 合成数据，非真实信号）" in report
    # 双簇双路径：种子域簇 L1 组合（O5）；跨域簇 L1 miss → L2 市场命中（O1）
    assert "combination（①组合）" in report and "| O5 |" in report
    assert "market（②市场）" in report and "| O1 |" in report
    assert "power-outage-query@1.0.0" in report  # L2 候选包引用
    # 逐级留痕（降路径可审计）+ 红线
    assert "L1 [miss] 无同域" in report
    assert "保持 draft" in report and "plugin 侧只读不装" in report
    assert "零幻觉上架" in report
    # 工单留痕：两簇各开一单（surface=O1 固定开单面）
    records = [
        line
        for line in (store_dir / "gap_proposals.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(records) == 2


def test_run_g1_demo_无市场_跨域簇如实降级(tmp_path: Path) -> None:
    store_dir = tmp_path / "store-nomarket"
    proc = _run(["--demo", "--no-demo-market", "--store-dir", str(store_dir)])
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "market=demo 合成市场" not in proc.stdout  # 未注入
    report = proc.stdout
    assert "未命中（L1:miss → L2:miss → L3:skipped）" in report  # 整链如实降级
    assert "L3 [skipped] model 未注入" in report
    assert "三级均未产出草案" in report


def test_run_g1_参数校验_双模式互斥_live须租户() -> None:
    for args in (
        ["--demo", "--live"],  # 互斥
        [],  # 必选其一
        ["--live"],  # live 缺租户
        ["--live", "--tenant-id", "not-a-uuid"],  # 租户非法 UUID
    ):
        proc = _run(args)
        assert proc.returncode != 0, f"args={args} 应拒绝"


# ---------------------------------------------------------------- live 装配面（ocr 评审 ①）


async def test_open_ledger_repo返回零参可重复调用工厂(monkeypatch) -> None:
    """LedgerFailureSink.open_repo 契约=工厂：drain 内 ``async with self._open_repo()`` 每次调用
    取新上下文——返回 ``_ctx()`` 实例（上下文管理器不可调用）时 drain 即 TypeError。"""
    import services.tools.orsi.run_g1 as run_g1

    engine_calls: list[str] = []
    session_opens: list[object] = []

    def fake_create_async_engine(dsn: str, **_: Any) -> object:
        engine_calls.append(dsn)
        return object()

    def fake_async_sessionmaker(engine: object, **_: Any):
        def factory():
            @asynccontextmanager
            async def session():
                session_opens.append(engine)
                yield object()

            return session()

        return factory

    class FakeRepo:
        def __init__(self, db: object, tenant_id: uuid.UUID) -> None:
            self.db = db
            self.tenant_id = tenant_id

    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", fake_create_async_engine)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.async_sessionmaker", fake_async_sessionmaker)
    monkeypatch.setattr(
        "services.writeback.data.repo_impl.writeback_repo.PgWritebackLedgerRepository", FakeRepo
    )

    tenant = uuid.UUID("00000000-0000-0000-0000-000000000001")
    factory = run_g1._open_ledger_repo(SimpleNamespace(pg_dsn="postgresql+asyncpg://fake"), tenant)
    assert callable(factory), "必须返回工厂而非上下文管理器实例"

    cm1, cm2 = factory(), factory()
    assert cm1 is not cm2  # 每次调用新上下文（drain 契约可多次进入）
    async with cm1 as repo1:
        assert isinstance(repo1, FakeRepo) and repo1.tenant_id == tenant
    async with cm2 as repo2:
        assert isinstance(repo2, FakeRepo)
    assert engine_calls == ["postgresql+asyncpg://fake"]  # 引擎仅组合根建一次
    assert len(session_opens) == 2  # 工厂两次进入两次开会话
