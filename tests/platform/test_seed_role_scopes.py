"""种子角色 scopes 补口迁移验证（a5b7c9d1e3f5）。

背景：SSE 双副本压测实勘发现 m1 种子 admin 角色缺 session:write/session:chat
（deploy/test/sse-dual-replica/README.md 前置条件 4「已知缺口」，真环境登录后
403+2001）；授权唯一归属=数据迁移（种子纪律：种子变更只走 Alembic 数据迁移，
README 的手工 UPDATE 仅压测期口径）。

验证口径：upgrade head 后 admin scopes 含两值且零重复（连跑两次验幂等）、
其他角色 scopes 不动；downgrade -1 后两值移除（零残留）；再 upgrade head 恢复。

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

    # ① upgrade head 连跑两次：幂等零重复
    for _ in range(2):
        _raise_or_skip(_run_alembic("upgrade", "head"))
    before = _roles_scopes(engine)
    assert "admin" in before, "种子 roles 未落库（upgrade head 未生效）"
    admin = before["admin"]
    for scope in _TARGET_SCOPES:
        assert scope in admin, f"upgrade 后 admin scopes 缺 {scope}"
        assert admin.count(scope) == 1, f"upgrade 后 admin scopes 中 {scope} 重复"

    # ② 其他角色 scopes 不动（WHERE code='admin' 隔离）
    snapshot = _roles_scopes(engine)
    assert all(snapshot[c] == before[c] for c in snapshot if c != "admin"), "非 admin 角色 scopes 被误改"

    # ③ downgrade -1：两值移除（零残留），其他角色仍不动
    _raise_or_skip(_run_alembic("downgrade", "-1"))
    after_down = _roles_scopes(engine)
    for scope in _TARGET_SCOPES:
        assert scope not in after_down["admin"], f"downgrade 后 admin scopes 残留 {scope}"
    assert all(after_down[c] == snapshot[c] for c in after_down if c != "admin")

    # ④ 再 upgrade head 恢复：两值回归且零重复
    _raise_or_skip(_run_alembic("upgrade", "head"))
    restored = _roles_scopes(engine)
    for scope in _TARGET_SCOPES:
        assert restored["admin"].count(scope) == 1, f"恢复后 admin scopes 中 {scope} 异常"
    assert all(restored[c] == snapshot[c] for c in restored if c != "admin")
