"""kb 五组新表迁移 b3d5f7a9c1e3 结构验证（information_schema 实库断言 + 往返）。

背景：五组 ORM（connector/governance/rule/usage）先行合入、registry 已登记，PG 侧
此前无表（测试全靠 aiosqlite 内存库绕过）；本迁移（改表流程第 3 步收口）落库后以
information_schema/pg_catalog 钉死列集、关键约束名与索引名——ORM 权威
（services/kb/data/{connector,governance,rule,usage}_orm.py）逐字转录，约束名以
Base.metadata CreateTable 编译产物为准（ck 短名被 naming convention 展开为
ck_kb_connector_events_<短名>；显式全名 ck_ 套一层得双前缀，kb_facts a9f3c2e1d7b8
迁移注释同款现象；uq/ix/pk/fk 模板不含 %(constraint_name)s，显式名直用）。

验证口径：钉死修订（upgrade b3d5f7a9c1e3 → 断言结构 → downgrade c9e3a7f1b5d2 →
五表零残留 → upgrade head 恢复）——规避 test_seed_vocab_scopes docstring 指出的
「downgrade -1 随 head 漂移」失效陷阱；head 漂移时会话间共享 PG 下 -1 可能降错目标。

跳过口径：本地 PG 不可达（connect 超时 3 秒，禁挂死套件）或 DB alembic_version 指向
的修订不在当前工作区脚本目录（多 worktree 错位）即整用例 skip
（tests/platform/test_seed_vocab_scopes.py 先例）。
"""

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from services.platform.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REVISION = "b3d5f7a9c1e3"
_DOWN_REVISION = "c9e3a7f1b5d2"
_TABLES = (
    "kb_connector_cursors",
    "kb_connector_events",
    "kb_fact_relations",
    "kb_conflicts",
    "kb_rule_candidates",
    "kb_usage_counters",
)

# 期望列集（逐字转录 ORM；条目 = (列名, data_type, character_maximum_length, nullable, column_default)）
# 默认值为 PG information_schema 落库形态（ORM Python default 经迁移 server_default 固化）。
_EXPECTED_COLUMNS: dict[str, set[tuple[str, str, int | None, bool, str | None]]] = {
    "kb_connector_cursors": {
        ("source_id", "character varying", 128, False, None),
        ("stream_id", "character varying", 256, False, "'*'::character varying"),
        ("cursor", "jsonb", None, False, "'{}'::jsonb"),
        ("connector_version", "character varying", 32, False, None),
        ("last_error", "text", None, True, None),
        ("id", "uuid", None, False, None),
        ("tenant_id", "uuid", None, False, None),
        ("created_at", "timestamp with time zone", None, False, "now()"),
        ("updated_at", "timestamp with time zone", None, False, "now()"),
    },
    "kb_connector_events": {
        ("source_type", "character varying", 16, False, None),
        ("source_id", "character varying", 128, False, None),
        ("source_system", "character varying", 64, False, None),
        ("stream_id", "character varying", 256, False, None),
        ("external_id", "character varying", 256, False, None),
        ("source_sequence", "bigint", None, False, None),
        ("event_type", "character varying", 16, False, None),
        ("payload_ref", "text", None, True, None),
        ("payload_hash", "character", 64, False, None),
        ("schema_fingerprint", "character varying", 64, False, None),
        ("connector_version", "character varying", 32, False, None),
        ("native_metadata", "jsonb", None, False, "'{}'::jsonb"),
        ("acl_tags", "jsonb", None, True, None),
        ("cursor", "jsonb", None, True, None),
        ("occurred_at", "timestamp with time zone", None, False, None),
        ("occurred_at_trust", "character varying", 16, False, None),
        ("collected_at", "timestamp with time zone", None, False, None),
        ("trace_id", "character varying", 64, True, None),
        ("id", "uuid", None, False, None),
        ("tenant_id", "uuid", None, False, None),
        ("created_at", "timestamp with time zone", None, False, "now()"),
        ("updated_at", "timestamp with time zone", None, False, "now()"),
    },
    "kb_fact_relations": {
        ("from_fact_id", "uuid", None, False, None),
        ("to_fact_id", "uuid", None, False, None),
        ("relation", "character varying", 32, False, None),
        ("evidence", "jsonb", None, False, "'{}'::jsonb"),
        ("id", "uuid", None, False, None),
        ("tenant_id", "uuid", None, False, None),
        ("created_at", "timestamp with time zone", None, False, "now()"),
        ("updated_at", "timestamp with time zone", None, False, "now()"),
    },
    "kb_conflicts": {
        ("conflict_type", "character varying", 2, False, None),
        ("fact_a_id", "uuid", None, False, None),
        ("fact_b_id", "uuid", None, False, None),
        ("score_a", "numeric", None, False, None),
        ("score_b", "numeric", None, False, None),
        ("resolution", "character varying", 16, False, "'pending'::character varying"),
        ("resolved_by", "uuid", None, True, None),
        ("comment", "text", None, True, None),
        ("id", "uuid", None, False, None),
        ("tenant_id", "uuid", None, False, None),
        ("created_at", "timestamp with time zone", None, False, "now()"),
        ("updated_at", "timestamp with time zone", None, False, "now()"),
    },
    "kb_rule_candidates": {
        ("document_id", "uuid", None, False, None),
        ("chunk_id", "uuid", None, True, None),
        ("rule_id", "character varying", 64, False, None),
        ("rule_key", "character varying", 64, False, None),
        ("kind", "character varying", 16, False, None),
        ("trigger", "text", None, False, None),
        ("consequence", "text", None, False, None),
        ("target_class", "character varying", 256, False, None),
        ("evidence", "jsonb", None, False, "'{}'::jsonb"),
        ("draft_shacl", "text", None, False, None),
        ("confidence", "numeric", None, False, None),
        ("risk_flag", "boolean", None, False, "true"),
        ("violations", "jsonb", None, False, "'[]'::jsonb"),
        ("status", "character varying", 16, False, "'candidate'::character varying"),
        ("trace_id", "character varying", 128, True, None),
        ("meta", "jsonb", None, False, "'{}'::jsonb"),
        ("id", "uuid", None, False, None),
        ("tenant_id", "uuid", None, False, None),
        ("created_at", "timestamp with time zone", None, False, "now()"),
        ("updated_at", "timestamp with time zone", None, False, "now()"),
    },
    "kb_usage_counters": {
        ("kb_collection_id", "uuid", None, False, None),
        ("chunk_id", "uuid", None, False, None),
        ("search_hits", "integer", None, False, "0"),
        ("action_refs", "integer", None, False, "0"),
        ("last_searched_at", "timestamp with time zone", None, True, None),
        ("last_action_at", "timestamp with time zone", None, True, None),
        ("id", "uuid", None, False, None),
        ("tenant_id", "uuid", None, False, None),
        ("created_at", "timestamp with time zone", None, False, "now()"),
        ("updated_at", "timestamp with time zone", None, False, "now()"),
    },
}

# 期望约束集（contype: p=PRIMARY KEY / u=UNIQUE / c=CHECK / f=FOREIGN KEY；名 = create_all 编译产物）
_EXPECTED_CONSTRAINTS: dict[str, set[tuple[str, str]]] = {
    "kb_connector_cursors": {
        ("p", "pk_kb_connector_cursors"),
        ("u", "uk_kb_connector_cursors_tenant_id_source_id_stream_id"),
    },
    "kb_connector_events": {
        ("p", "pk_kb_connector_events"),
        ("c", "ck_kb_connector_events_source_type"),
        ("c", "ck_kb_connector_events_event_type"),
        ("c", "ck_kb_connector_events_occurred_at_trust"),
        ("u", "uk_kb_connector_events_dedup"),
    },
    "kb_fact_relations": {
        ("p", "pk_kb_fact_relations"),
        ("c", "ck_kb_fact_relations_ck_kb_fact_relations_relation"),
        ("u", "uk_kb_fact_relations_from_to_relation"),
        ("f", "fk_kb_fact_relations_from_fact_id_kb_facts"),
        ("f", "fk_kb_fact_relations_to_fact_id_kb_facts"),
    },
    "kb_conflicts": {
        ("p", "pk_kb_conflicts"),
        ("c", "ck_kb_conflicts_ck_kb_conflicts_conflict_type"),
        ("c", "ck_kb_conflicts_ck_kb_conflicts_resolution"),
        ("u", "uk_kb_conflicts_fact_a_id_fact_b_id"),
        ("f", "fk_kb_conflicts_fact_a_id_kb_facts"),
        ("f", "fk_kb_conflicts_fact_b_id_kb_facts"),
        ("f", "fk_kb_conflicts_resolved_by_users"),
    },
    "kb_rule_candidates": {
        ("p", "pk_kb_rule_candidates"),
        ("c", "ck_kb_rule_candidates_ck_kb_rule_candidates_kind"),
        ("c", "ck_kb_rule_candidates_ck_kb_rule_candidates_status"),
        ("c", "ck_kb_rule_candidates_ck_kb_rule_candidates_risk_flag_true"),
        ("u", "uk_kb_rule_candidates_tenant_id_rule_key"),
        ("f", "fk_kb_rule_candidates_document_id_documents"),
        ("f", "fk_kb_rule_candidates_chunk_id_document_chunks"),
    },
    "kb_usage_counters": {
        ("p", "pk_kb_usage_counters"),
        ("u", "uk_kb_usage_counters_dims"),
    },
}

# 期望索引集（uk 约束自带的同名 PG 索引不计入；ORM __table_args__ Index + TenantMixin index=True）
_EXPECTED_INDEXES: dict[str, set[str]] = {
    "kb_connector_cursors": {"ix_kb_connector_cursors_tenant_id"},
    "kb_connector_events": {
        "ix_kb_connector_events_tenant_id",
        "ix_kb_connector_events_trace",
        "ix_kb_connector_events_collected",
    },
    "kb_fact_relations": {"ix_kb_fact_relations_tenant_id", "ix_kb_fact_relations_to"},
    "kb_conflicts": {"ix_kb_conflicts_tenant_id", "ix_kb_conflicts_queue"},
    "kb_rule_candidates": {"ix_kb_rule_candidates_tenant_id", "idx_kb_rule_candidates_doc"},
    "kb_usage_counters": {"ix_kb_usage_counters_tenant_id", "ix_kb_usage_counters_hits"},
}


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "services/alembic.ini", *args],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )


def _raise_or_skip(r: subprocess.CompletedProcess[str]) -> None:
    if r.returncode != 0 and "Can't locate revision" in (r.stdout + r.stderr):
        pytest.skip("DB alembic_version 指向的修订不在当前工作区脚本目录（环境错位），跳过")
    assert r.returncode == 0, r.stderr


def test_kb_governance_5_tables_schema_roundtrip():
    engine = create_engine(Settings().pg_dsn, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except (OSError, SQLAlchemyError):
        pytest.skip("本地 PG 不可达，跳过 kb 五表迁移结构验证（口径：环境不可达即跳过）")

    # ① upgrade 钉死本迁移：五表落库
    _raise_or_skip(_run_alembic("upgrade", _REVISION))

    # ② 列集断言（列名/data_type/长度/nullable/server_default 五元组全量比对）
    with engine.connect() as conn:
        for table in _TABLES:
            rows = conn.execute(
                text(
                    "SELECT column_name, data_type, character_maximum_length, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_name = :t"
                ),
                {"t": table},
            ).fetchall()
            actual = {
                (c, dt, ml, is_nullable == "YES", d) for c, dt, ml, is_nullable, d in rows
            }
            expected = _EXPECTED_COLUMNS[table]
            missing = expected - actual
            unexpected = actual - expected
            assert not missing, f"{table} 缺列/列属性不符：{sorted(missing)}"
            assert not unexpected, f"{table} 多出列/列属性漂移：{sorted(unexpected)}"

        # numeric(4,3) 精度抽核（score_a/score_b/confidence：data_type 断言探不出 precision/scale）
        for table, col in (
            ("kb_conflicts", "score_a"),
            ("kb_conflicts", "score_b"),
            ("kb_rule_candidates", "confidence"),
        ):
            row = conn.execute(
                text(
                    "SELECT numeric_precision, numeric_scale FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": col},
            ).fetchone()
            assert row == (4, 3), f"{table}.{col} 应为 numeric(4,3)，实得 {row}"

        # ③ 约束名断言（pg_catalog：p/u/c/f 四类全量比对）
        for table in _TABLES:
            rows = conn.execute(
                text(
                    "SELECT contype, conname FROM pg_constraint con "
                    "JOIN pg_class rel ON rel.oid = con.conrelid "
                    "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
                    "WHERE rel.relname = :t AND nsp.nspname = 'public'"
                ),
                {"t": table},
            ).fetchall()
            actual = set(rows)
            expected = _EXPECTED_CONSTRAINTS[table]
            missing = expected - actual
            unexpected = actual - expected
            assert not missing, f"{table} 缺约束/约束名不符：{sorted(missing)}"
            assert not unexpected, f"{table} 多出约束：{sorted(unexpected)}"

        # ④ 索引名断言（显式建索引全集须在；uk 约束自动同名索引不参与）
        for table in _TABLES:
            rows = conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = :t AND schemaname = 'public'"),
                {"t": table},
            ).fetchall()
            actual = {r[0] for r in rows}
            missing = _EXPECTED_INDEXES[table] - actual
            assert not missing, f"{table} 缺索引：{sorted(missing)}"

    # ⑤ downgrade 钉死前驱：五表零残留（信息架构双探：tables + pg_class）
    _raise_or_skip(_run_alembic("downgrade", _DOWN_REVISION))
    with engine.connect() as conn:
        leftovers = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY(:ts)"
            ),
            {"ts": list(_TABLES)},
        ).fetchall()
    assert not leftovers, f"downgrade 后五表残留：{leftovers}"

    # ⑥ 再 upgrade head 往返收口（恢复到本迁移；head 漂移由 _raise_or_skip 兜底留痕）
    _raise_or_skip(_run_alembic("upgrade", "head"))
    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY(:ts)"
            ),
            {"ts": list(_TABLES)},
        ).scalar()
    assert count == len(_TABLES), f"往返后五表未全部恢复：{count}/{len(_TABLES)}"
