"""种子角色 scopes 补口迁移验证（a5b7c9d1e3f5）。

背景：SSE 双副本压测实勘发现 m1 种子 admin 角色缺 session:write/session:chat
（deploy/test/sse-dual-replica/README.md 前置条件 4「已知缺口」，真环境登录后
403+2001）；授权唯一归属=数据迁移（种子纪律：种子变更只走 Alembic 数据迁移，
README 的手工 UPDATE 仅压测期口径）。

验证口径（2026-10-04 ocr 整改钉死修订号：head 上移（c9e3a7f1b5d2）后原「downgrade -1」
随 head 漂移不再指向本迁移，两值零残留断言失效——同 test_seed_vocab_scopes 先例口径，
两端均钉死在本迁移切片 [d4f6a8c2e9b1, a5b7c9d1e3f5] 内，不受后续 head 上移影响；
DB 先归位到切片顶再断言）：
upgrade a5b7c9d1e3f5 后 admin scopes 含两值且零重复（连跑两次验幂等）、
其他角色 scopes 不动；downgrade d4f6a8c2e9b1 后两值移除（零残留）；再 upgrade 恢复
（终态=DB 停在本迁移修订，后续测试自行归位）。

跳过口径：本地 PG 不可达即整文件 skip（同 tests/platform/conftest.py llm_seed
夹具与 tests/integration/test_seed_idempotent.py 的「环境不可达即跳过」纪律）；
DB alembic_version 指向的修订不在当前工作区脚本目录（多 worktree 错位）同款 skip。
"""

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from services.platform.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REVISION = "a5b7c9d1e3f5"
_DOWN_REVISION = "d4f6a8c2e9b1"
_TARGET_SCOPES = ("session:write", "session:chat")


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )


def _raise_or_skip(r: subprocess.CompletedProcess[str]) -> None:
    if r.returncode != 0 and "Can't locate revision" in (r.stdout + r.stderr):
        pytest.skip("DB alembic_version 指向的修订不在当前工作区脚本目录（环境错位），跳过")
    assert r.returncode == 0, r.stderr


def _normalize_to_revision_top() -> None:
    """归位 DB 到本迁移修订（切片顶）：钉死目标低于当前时 upgrade 是无操作（rc=0），
    须再显式 downgrade 归位；已在目标修订时 downgrade 报「not a valid downgrade
    target」——该无操作报错按成功放行（实勘 2026-10-04，本仓库 alembic 版本行为）。"""
    _raise_or_skip(_run_alembic("upgrade", _REVISION))
    r = _run_alembic("downgrade", _REVISION)
    if r.returncode != 0 and "not a valid downgrade target" not in (r.stdout + r.stderr):
        pytest.fail(f"alembic downgrade {_REVISION} 归位失败：{r.stderr}")


def _roles_scopes(engine) -> dict[str, list[str]]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT code, scopes FROM roles")).fetchall()
    return {code: list(scopes) for code, scopes in rows}


def test_admin_session_scopes_roundtrip():
    engine = create_engine(Settings().pg_dsn)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except (OSError, SQLAlchemyError):
        pytest.skip("本地 PG 不可达，跳过种子角色 scopes 迁移验证（口径：环境不可达即跳过）")

    # ① 归位到本迁移切片顶（钉死修订，两种进入态<低于/高于切片>都收敛），随后
    #    连跑两次 upgrade（皆无操作，保持与原「连跑两次」口径一致）
    _normalize_to_revision_top()
    for _ in range(2):
        _raise_or_skip(_run_alembic("upgrade", _REVISION))
    before = _roles_scopes(engine)
    assert "admin" in before, "种子 roles 未落库（upgrade 未生效）"
    admin = before["admin"]
    for scope in _TARGET_SCOPES:
        assert scope in admin, f"upgrade 后 admin scopes 缺 {scope}"
        assert admin.count(scope) == 1, f"upgrade 后 admin scopes 中 {scope} 重复"

    # ② 其他角色 scopes 不动（WHERE code='admin' 隔离）
    snapshot = _roles_scopes(engine)
    assert all(snapshot[c] == before[c] for c in snapshot if c != "admin"), "非 admin 角色 scopes 被误改"

    # ③ downgrade 钉死前驱修订（=只跑本迁移 downgrade）：两值移除（零残留），其他角色仍不动
    _raise_or_skip(_run_alembic("downgrade", _DOWN_REVISION))
    after_down = _roles_scopes(engine)
    for scope in _TARGET_SCOPES:
        assert scope not in after_down["admin"], f"downgrade 后 admin scopes 残留 {scope}"
    assert all(after_down[c] == snapshot[c] for c in after_down if c != "admin")

    # ④ 再 upgrade 恢复（终态=DB 停在本迁移修订，后续测试自行归位）：两值回归且零重复
    _raise_or_skip(_run_alembic("upgrade", _REVISION))
    restored = _roles_scopes(engine)
    for scope in _TARGET_SCOPES:
        assert restored["admin"].count(scope) == 1, f"恢复后 admin scopes 中 {scope} 异常"
    assert all(restored[c] == snapshot[c] for c in restored if c != "admin")
