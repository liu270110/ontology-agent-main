"""add sandbox 5 tables (domain-10)

Revision ID: c8d5e2f7a931
Revises: f0f79f84dce4
Create Date: 2026-09-27

Discipline (arch 06 / database/01): additive-only; single head.
DDL 权威 = database/01 §3.10——迁移用原文 DDL 执行，避免 ORM API 翻译漂移；
离线干跑：alembic upgrade head --sql（PG 活库不可用时）。
"""

from typing import Sequence, Union

from alembic import op


revision: str = "c8d5e2f7a931"
down_revision: Union[str, None] = "f0f79f84dce4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DDL = [
    """
    CREATE TABLE egress_policies (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id),
        name VARCHAR(128) NOT NULL, version INT NOT NULL DEFAULT 1,
        rules JSONB NOT NULL DEFAULT '[]',
        review_ticket_id UUID REFERENCES review_tickets(id),
        enabled BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uk_egress_policies_tenant_name_ver UNIQUE (tenant_id, name, version))
    """,
    """
    CREATE TABLE sandbox_profiles (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID REFERENCES tenants(id),
        name VARCHAR(128) NOT NULL, scenario VARCHAR(4) NOT NULL CHECK (scenario IN ('S0','S1','S2','S3','S4','S5')),
        trust_level VARCHAR(2) NOT NULL CHECK (trust_level IN ('T0','T1','T2','T3')),
        cpu_limit NUMERIC(4,2) NOT NULL DEFAULT 0.5, mem_limit_mb INT NOT NULL DEFAULT 512,
        pids_limit INT NOT NULL DEFAULT 256, output_quota_kb INT NOT NULL DEFAULT 10240,
        egress_policy_id UUID REFERENCES egress_policies(id),
        base_image VARCHAR(256) NOT NULL DEFAULT 'docker.m.daocloud.io/library/python:3.12-slim',
        toolkit_ref JSONB NOT NULL DEFAULT '{}',
        qos VARCHAR(2) NOT NULL DEFAULT 'ls' CHECK (qos IN ('ls','be')),
        is_builtin BOOLEAN NOT NULL DEFAULT false,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uk_sandbox_profiles_tenant_name UNIQUE (tenant_id, name))
    """,
    """
    CREATE TABLE sandbox_instances (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id),
        profile_id UUID NOT NULL REFERENCES sandbox_profiles(id),
        owner_kind VARCHAR(16) NOT NULL CHECK (owner_kind IN ('session','pipeline','plugin','eval_run')),
        owner_ref UUID NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'creating'
            CHECK (status IN ('creating','ready','running','paused','hibernated','failed','terminated')),
        backend VARCHAR(16) NOT NULL DEFAULT 'docker', instance_ref VARCHAR(128),
        workspace_volume VARCHAR(128), credential_jti UUID,
        last_heartbeat_at TIMESTAMPTZ, cpu_used NUMERIC(6,3), mem_used_mb INT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())
    """,
    """
    CREATE TABLE sandbox_snapshots (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id),
        instance_id UUID NOT NULL REFERENCES sandbox_instances(id),
        object_key VARCHAR(512) NOT NULL, size_bytes BIGINT,
        source VARCHAR(16) NOT NULL DEFAULT 'hibernate' CHECK (source IN ('hibernate','checkpoint','artifact')),
        sanitized BOOLEAN NOT NULL DEFAULT false, retain_until TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())
    """,
    """
    CREATE TABLE sandbox_events (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id),
        instance_id UUID REFERENCES sandbox_instances(id), event VARCHAR(32) NOT NULL,
        detail JSONB NOT NULL DEFAULT '{}', trace_id VARCHAR(64),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())
    """,
]


def upgrade() -> None:
    for stmt in _DDL:
        op.execute(stmt)


def downgrade() -> None:
    for tbl in ("sandbox_events", "egress_policies", "sandbox_snapshots", "sandbox_instances", "sandbox_profiles"):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
