"""种子迁移幂等集成测试（验收遗留#4）：upgrade 连跑两次零重复。

前置：本地 PG（deploy compose）。跳过条件：DB alembic_version 指向的修订不在当前
工作区脚本目录（多 worktree / 他会话未提交迁移并存的环境错位——该失败不属于本用例
的幂等契约，跳过并留痕；同 tests/kb 夹具「环境不可达即跳过」纪律）。
"""

import subprocess
import sys

import pytest


def test_seed_migration_idempotent():
    for _ in range(2):  # 连续两次 upgrade：第二次必须 no-op 成功
        r = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "services/alembic.ini", "upgrade", "head"],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0 and "Can't locate revision" in (r.stdout + r.stderr):
            pytest.skip("DB alembic_version 指向的修订不在当前工作区脚本目录（环境错位），跳过种子幂等用例")
        assert r.returncode == 0, r.stderr
