"""ONT-2 能力本体种子（数据迁移）：cap TBox 项目 + A 档 21 能力行 × 定义快照回填。

2026-10-07 ONT-2 批（契约=docs/architecture/06 篇 §ONT-2.4「种子唯一走 Alembic 数据迁移，
种子由代码内清单生成——不复制粘贴 schema，保单源」）：

- 全部种子逻辑单点 = services/ontology/business/capability_seed.py（纯 builder + 异步链）：
  装载自证门禁（lint + SHACL Violation 清零 + TBox/参数形状与代码常量对账，漂移即拒）→
  建「平台能力本体」项目行（nil 系统租户，iri_base=契约命名空间 http://ontology-agent.local/cap#）
  → 变更单五动词链（solo 档系统自审）→ append_version（cap.ttl 为 v1 制品）→ publish →
  读模型四表投影 → 21 能力行（16 工具读 services.agent/mcp 代码常量 + 5 业务行动解析
  power 种子 TTL），每行 source='seed'，同事务经 ONT-1 快照服务
  （OntologyElementVersionRepo.snapshot，element_type='capability'）落定义快照并回填
  current_definition_version_id。四层判重（本体行/版本 checksum/能力行 (version_id, iri)/
  快照 hash）——重跑不重复（幂等为迁移硬要求）。
- 编程模型（侦察批上报的「迁移编程模型冲突」裁决落法）：本迁移是舰队首个 import services.*
  与 asyncio 的迁移——同步 Alembic 连接无法承载 async 仓储（AsyncSession 需 async 驱动），
  故以独立 async 引擎驱动单事务种子链（run_seed）；
  `op.get_bind()` 先行 COMMIT 逃逸：同批首跑时迁移① 的 DDL 须先落库对种子连接可见
  （alembic 缺省全程单事务；COMMIT 后 alembic 隐式续新事务，版本戳照常终态提交）。
  崩溃窗口语义：COMMIT 落库后、版本戳提交前失败/崩溃 → 重跑=迁移①（存在性守卫可重入，
  见 ① 注记）干净跳过 + 本种子幂等重放，无孤儿不卡链；COMMIT 前失败 → 外层事务整体回滚，
  ① 的 DDL 一并未落，重放从零开始。
- importlinter：「platform 底座零上层依赖」契约豁免边随本批登记 pyproject
  ignore_imports（组合点白名单同款，TODO(M4) 收口复核）。

Revision ID: f4d6b8e0a2c4
Revises: e2c4a6f8d0b2
Create Date: 2026-10-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f4d6b8e0a2c4"
down_revision: str | None = "e2c4a6f8d0b2"  # 串回本批 schema 迁移之后（线性：K28→e2c4→f4d6）
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 同批首跑：迁移① 的 DDL 尚在 alembic 外层事务未提交，种子独立连接不可见——先提交逃逸
    # （仅本迁移内有写面前提；单独重放时为空事务 COMMIT，无副作用）。
    op.get_bind().exec_driver_sql("COMMIT")

    from services.ontology.business.capability_seed import run_seed

    report = run_seed()
    print(
        f"[ont2_seed] ontology={report.ontology_id} version={report.version} "
        f"rows={report.rows_inserted}+{report.rows_skipped} snapshots={report.snapshots_taken} "
        f"version_created={report.version_created}",
    )


def downgrade() -> None:
    # upgrade-only（本仓惯例）：种子数据不回滚（幂等重放语义，M46 终局收口迁移同款纪律）。
    pass
