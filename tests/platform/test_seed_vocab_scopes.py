"""种子角色既有词表 scopes 补口迁移验证（c9e3a7f1b5d2，联调缺陷台账 2026-10-04 B3）。

背景：admin/super_admin 种子缺配既有词表内已在代码侧强制（require_scope）的四个 scope
（prompt:read/admin:read/admin:write/plugin:install，真环境 admin 登录访问提示词库、
回写台账、插件安装端点 403+2001）；授权唯一归属=数据迁移（种子纪律：种子变更只走
Alembic 数据迁移；先例 f0f79f84dce4 / a5b7c9d1e3f5）。

验证口径（**钉死修订号**，规避 a5b7c9d1e3f5 先例测试「downgrade -1 随 head 漂移」的
失效陷阱——head 上移后 -1 不再指向目标迁移）：upgrade c9e3a7f1b5d2 后 admin 与
super_admin 均含四值且零重复、既有 scopes 无丢失（超集校验）；upgrade SQL 复跑一次
验幂等（集合不变）；downgrade b7d2e4f6a8c0 后**按角色区分目标集**（ocr 整改
2026-10-04：admin 四值零残留；super_admin 仅本迁移实际新增的三值零残留，m1 种子
f0f79f84dce4 既有的 plugin:install 保留——先例 a5b7c9d1e3f5「downgrade 只移除本迁移
upgrade 实际新增的」口径）；再 upgrade 恢复。非目标角色
（member/ontologist/curator/guest 等）scopes 全程不动。

跳过口径：本地 PG 不可达即整文件 skip；DB alembic_version 指向的修订不在当前工作区
脚本目录（多 worktree 错位）同款 skip（tests/platform/test_seed_role_scopes.py 先例）。
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from services.platform.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = (
    _REPO_ROOT
    / "services"
    / "platform"
    / "db"
    / "migrations"
    / "versions"
    / "20261004_c9e3a7f1b5d2_seed_admin_vocab_scopes.py"
)
_REVISION = "c9e3a7f1b5d2"
_DOWN_REVISION = "b7d2e4f6a8c0"
_TARGET_ROLES = ("admin", "super_admin")
_TARGET_SCOPES = ("prompt:read", "admin:read", "admin:write", "plugin:install")
# 本迁移对 super_admin 实际新增的三值（plugin:install 为 m1 种子 f0f79f84dce4 既有授权，
# 不在其列——downgrade 不得回收，ocr 整改 2026-10-04 + 先例 a5b7c9d1e3f5 口径）
_SUPER_ADMIN_ADDED_SCOPES = ("prompt:read", "admin:read", "admin:write")


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


def _load_upgrade_sql() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location("_seed_vocab_scopes_migration", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None, f"迁移脚本缺失：{_MIGRATION_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    assert module.revision == _REVISION
    # 同仓测试直读迁移常量（幂等复跑口径）；per-role 两条 UPDATE，逐条执行
    return module._UPGRADE_SQL  # noqa: SLF001


def _roles_scopes(engine) -> dict[str, list[str]]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT code, scopes FROM roles")).fetchall()
    return {code: list(scopes) for code, scopes in rows}


def test_admin_super_admin_vocab_scopes_roundtrip():
    engine = create_engine(Settings().pg_dsn)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except (OSError, SQLAlchemyError):
        pytest.skip("本地 PG 不可达，跳过种子角色 scopes 迁移验证（口径：环境不可达即跳过）")

    # ① upgrade 钉死修订：admin/super_admin 含四值且零重复（连跑两次验迁移可重入）
    for _ in range(2):
        _raise_or_skip(_run_alembic("upgrade", _REVISION))
    after_up = _roles_scopes(engine)
    for role in _TARGET_ROLES:
        assert role in after_up, f"种子 roles 未落库（upgrade {_REVISION} 未生效）"
        scopes = after_up[role]
        for scope in _TARGET_SCOPES:
            assert scope in scopes, f"upgrade 后 {role} scopes 缺 {scope}"
            assert scopes.count(scope) == 1, f"upgrade 后 {role} scopes 中 {scope} 重复"

    # ② 非 target 角色 scopes 不动（WHERE code IN ('admin','super_admin') 隔离）
    baseline = _roles_scopes(engine)
    assert all(baseline[c] == after_up[c] for c in baseline if c not in _TARGET_ROLES), (
        "非 admin/super_admin 角色 scopes 被误改"
    )

    # ③ 幂等：upgrade SQL 直跑一次（不经 alembic 版本号），scopes 集合不变
    with engine.begin() as conn:
        for stmt in _load_upgrade_sql():
            conn.execute(text(stmt))
    after_rerun = _roles_scopes(engine)
    for role in _TARGET_ROLES:
        assert set(after_rerun[role]) == set(baseline[role]), f"{role} 重复 upgrade 产生 scopes 漂移"

    # ④ 既有 scopes 零丢失：upgrade 只做并集（super_admin 种子原有 plugin:install 等仍在）
    #    以 m1 种子（f0f79f84dce4）锚点 scope 抽样核验超集关系
    for role, anchor in (("admin", "review:approve"), ("super_admin", "plugin:publish")):
        assert anchor in after_rerun[role], f"upgrade 后 {role} 既有 scope {anchor} 丢失"

    # ⑤ downgrade 钉死前驱修订，按角色区分目标集（ocr 整改 2026-10-04 + 先例
    #    a5b7c9d1e3f5「downgrade 只移除本迁移 upgrade 实际新增的」）：admin 四值零残留；
    #    super_admin 仅三值（本迁移实际新增集）零残留，m1 种子（f0f79f84dce4）既有的
    #    plugin:install 保留——不得回收迁移前已存在的授权
    _raise_or_skip(_run_alembic("downgrade", _DOWN_REVISION))
    after_down = _roles_scopes(engine)
    for scope in _TARGET_SCOPES:
        assert scope not in after_down["admin"], f"downgrade 后 admin scopes 残留 {scope}"
    for scope in _SUPER_ADMIN_ADDED_SCOPES:
        assert scope not in after_down["super_admin"], f"downgrade 后 super_admin scopes 残留 {scope}"
    assert "plugin:install" in after_down["super_admin"], (
        "downgrade 误回收 super_admin 既有 plugin:install（m1 种子 f0f79f84dce4 授权）"
    )
    assert all(after_down[c] == baseline[c] for c in after_down if c not in _TARGET_ROLES), (
        "downgrade 误改非 target 角色 scopes"
    )

    # ⑥ 再 upgrade 恢复：四值回归且零重复（终态=DB 停在 _REVISION，不干扰后续迁移）
    _raise_or_skip(_run_alembic("upgrade", _REVISION))
    restored = _roles_scopes(engine)
    for role in _TARGET_ROLES:
        assert set(restored[role]) == set(baseline[role]), f"恢复后 {role} scopes 与 upgrade 终态不一致"
