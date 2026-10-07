"""add workflows + workflow_versions 两表 & review_tickets target_type 增 workflow_publish

Revision ID: f1a9c3e5b7d2
Revises: a4b1cc126df7
Create Date: 2026-10-07

F1 workflows 域第一竖切（docs/Agent/15-下一波后端推进设计（主仓本地）§1.2；上游=27 篇 §3）：

1. ``workflows``：工作流主表（id/tenant_id/name/description/template/status 三态
   draft|published|deprecated/draft JSONB（nodes/edges）/head_version/created_by/审计列）；
2. ``workflow_versions``：不可变版本行（workflow_id/version/snapshot JSONB/note/
   published_by/published_at，uk(workflow_id,version)）；版本一经落库零更新路径（15 §1.1）。
3. ``review_tickets.target_type`` CHECK 枚举增 ``workflow_publish``（第七类对象候选——
   27 篇 §3/§5，X16 挂账 11 篇裁决；工单批准后回迁 head=后续批，v1 工单仅承载审批记录）。
   CHECK 约束不支持 ALTER，按 drop + create 原位重建（约束名 ck_review_tickets_target_type
   不变；20260929_d3f6a9c1e2b7 conflict 扩展先例同款）。

迁移链注意（S 批纪律：agents 不处理跨批迁移链）：2026-10-07 实测 develop 双头在途——
a4b1cc126df7（merge_heads_c1 链与并行批次）与 a3f5b8d1c7e9（add_tools_registrant）。
本迁移 down_revision 取其中合并面更宽的 a4b1cc126df7；a3f5b8d1c7e9 仍为并行头，合入时
主会话调链照旧（15 §1.2「合入时主会话调链照旧」）。

additive-only；downgrade：先删两表再收窄回七值枚举（workflow_publish 工单先由调用方
清理，约束失败即中止回滚）。

ORM parity: services/workflows/data/orm.py、services/review/data/orm.py ReviewTicket.target_type。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f1a9c3e5b7d2"
down_revision: str | None = "a4b1cc126df7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TARGET_TYPES_V8 = (
    "target_type IN ('ontology_candidate','knowledge_instance','memory_l2_upgrade',"
    "'plugin_listing','writeback_incident','conflict','permission_request','workflow_publish')"
)
# 收窄回本迁移前词汇（20261005_c5e9a1d3b7f5 admin 域批已并入 permission_request）
_TARGET_TYPES_V7 = (
    "target_type IN ('ontology_candidate','knowledge_instance','memory_l2_upgrade',"
    "'plugin_listing','writeback_incident','conflict','permission_request')"
)

_DDL = [
    """
    CREATE TABLE workflows (                        -- 工作流主表（15 §1.2；租户级）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id),
        name VARCHAR(128) NOT NULL, description VARCHAR(512) NOT NULL DEFAULT '',
        template VARCHAR(64) NOT NULL DEFAULT 'blank',     -- 实例化来源模板（详情回显）
        status VARCHAR(16) NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','published','deprecated')),   -- 15 §1.2 三态
        draft JSONB NOT NULL DEFAULT '{"nodes": [], "edges": []}',  -- 草稿图 {nodes, edges}
        head_version INTEGER CHECK (head_version IS NULL OR head_version >= 1),  -- None=从未发布
        created_by UUID REFERENCES users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE workflow_versions (                -- 不可变版本行（15 §1.2；uk(workflow_id,version)）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id),
        workflow_id UUID NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
        version INTEGER NOT NULL CHECK (version >= 1),
        snapshot JSONB NOT NULL,                    -- {nodes, edges} 固化形（不可变）
        note VARCHAR(512) NOT NULL DEFAULT '',
        published_by UUID REFERENCES users(id),
        published_at TIMESTAMPTZ,
        CONSTRAINT uk_workflow_versions_wf_version UNIQUE (workflow_id, version)
    )
    """,
    "CREATE INDEX ix_workflows_tenant_id ON workflows (tenant_id)",
    "CREATE INDEX ix_workflow_versions_tenant_id ON workflow_versions (tenant_id)",
    "CREATE INDEX ix_workflow_versions_workflow_id ON workflow_versions (workflow_id)",
]


def upgrade() -> None:
    for stmt in _DDL:
        op.execute(stmt)
    _CK_NAME = op.f("ck_review_tickets_target_type")
    op.drop_constraint(_CK_NAME, "review_tickets", type_="check")
    op.create_check_constraint(_CK_NAME, "review_tickets", _TARGET_TYPES_V8)


def downgrade() -> None:
    _CK_NAME = op.f("ck_review_tickets_target_type")
    op.drop_constraint(_CK_NAME, "review_tickets", type_="check")
    op.create_check_constraint(_CK_NAME, "review_tickets", _TARGET_TYPES_V7)
    op.execute("DROP TABLE IF EXISTS workflow_versions CASCADE")
    op.execute("DROP TABLE IF EXISTS workflows CASCADE")
