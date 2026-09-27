"""种子迁移幂等集成测试（验收遗留#4）：upgrade 连跑两次零重复。

前置：本地 PG（deploy compose）。跳过条件：无 OA_PG_HOST 可达（CI lite 段执行）。
"""

import subprocess
import sys


def test_seed_migration_idempotent():
    for _ in range(2):  # 连续两次 upgrade：第二次必须 no-op 成功
        r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
