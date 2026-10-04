"""kb 域 ORM FK 列显式类型回归（零 PG 真连）。

背景与实证（2026-10-04，SQLAlchemy 2.0.44）：kb 域曾有 17 个 FK 列用
``mapped_column(ForeignKey(...))`` 裸模式缺显式类型（governance_orm 5 + rule_orm 2 +
orm.py 10），仅靠 ``Mapped[uuid.UUID]`` 注解推断兜底。实测推断结果 =
``sqlalchemy.sql.sqltypes.UUID``（2.0.44 中与 ``postgresql.UUID`` 同一类、as_uuid 默认
True），PG 方言暂可编译、非 NullType——但推断属版本行为：注解缺失或推断策略变化即
NullType 化（无注解裸 FK 列可为 NullType；NullType 列在 aiosqlite 合法而掩盖，PG
create_all 链路即编译风险）。修复 = 全部显式化 ``UUID(as_uuid=True)``（基座范式
services/platform/db/base.py PkMixin/TenantMixin 同款）。

两层断言：
- AST 静态层：kb ORM 源码禁止 ``mapped_column(ForeignKey(...))`` 裸形态（显式类型必须
  位于 ForeignKey 之前）——修复前红（17 列）、修复后绿，防复发；
- 运行时层：全 kb 表 PG 方言 CreateTable 编译 + 逐列非 NullType + FK 列类型
  isinstance(postgresql.UUID) 且 as_uuid=True（与基座同款）+ DDL 无裸 NULL 类型渲染。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.sqltypes import NullType

KB_DATA_DIR = Path(__file__).resolve().parents[2] / "services" / "kb" / "data"
KB_ORM_MODULES = (
    "services.kb.data.orm",
    "services.kb.data.governance_orm",
    "services.kb.data.rule_orm",
    "services.kb.data.maintenance_orm",
    "services.kb.data.usage_orm",
    "services.kb.data.connector_orm",
)
# 本修复直接涉及的三张表（治理两表 + 规则候选表）；主表 kb_facts/documents 作类型对照
REPAIRED_TABLES = ("kb_conflicts", "kb_fact_relations", "kb_rule_candidates")


def _kb_tables() -> dict[str, object]:
    """聚合注册后按 kb ORM 模块收集表对象（新表自动纳入，不需改本测试）。"""
    importlib.import_module("services.platform.db.registry")  # noqa: F401 聚合注册副作用
    from services.platform.db.base import Base

    tables: dict[str, object] = {}
    for modname in KB_ORM_MODULES:
        mod = importlib.import_module(modname)
        for obj in vars(mod).values():
            if isinstance(obj, type) and issubclass(obj, Base) and hasattr(obj, "__tablename__"):
                tables[obj.__tablename__] = obj.__table__
    assert len(tables) >= 15, f"kb 域表数异常（聚合注册失效？）: {len(tables)}"
    return tables


def _fk_columns(table: object) -> list[object]:
    return [col for col in table.columns if col.foreign_keys]  # type: ignore[attr-defined]


def _bare_fk_calls(path: Path) -> list[str]:
    """AST 扫描 mapped_column 首位置参数即 ForeignKey 的裸形态（缺显式类型）。"""
    bare: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "mapped_column"
            and node.args
            and isinstance(node.args[0], ast.Call)
            and isinstance(node.args[0].func, ast.Name)
            and node.args[0].func.id == "ForeignKey"
        ):
            bare.append(f"{path.name}:{node.lineno}")
    return bare


def test_kb_orm_sources_have_no_bare_fk_mapped_column() -> None:
    """AST 静态：kb ORM 源码禁止裸 ForeignKey 列（修复前 17 处红，修复后须恒绿防复发）。"""
    bare: list[str] = []
    for path in sorted(KB_DATA_DIR.glob("*.py")):
        if path.name != "__init__.py":
            bare.extend(_bare_fk_calls(path))
    assert not bare, f"裸 ForeignKey 列（缺显式类型，NullType 风险）: {bare}"


def test_all_kb_tables_compile_on_pg_dialect_without_null_type() -> None:
    """全 kb 表 PG 方言编译 + 逐列非 NullType + DDL 无裸 NULL 类型渲染（零真连）。"""
    for name, table in sorted(_kb_tables().items()):
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))  # NullType 化即编译断点
        for col in table.columns:  # type: ignore[attr-defined]
            assert not isinstance(col.type, NullType), f"{name}.{col.name}: 列类型为 NullType"
            assert str(col.type) != "NULL", f"{name}.{col.name}: DDL 渲染出裸 NULL 类型"
        assert ddl.strip().startswith(f"CREATE TABLE {name}")


def test_all_kb_fk_columns_use_base_uuid_style() -> None:
    """全部 kb 表 FK 列类型 = postgresql.UUID(as_uuid=True)（基座 PkMixin/TenantMixin 同款）。"""
    for name, table in sorted(_kb_tables().items()):
        for col in _fk_columns(table):
            assert isinstance(col.type, PGUUID) and col.type.as_uuid is True, (
                f"{name}.{col.name}: FK 列类型须 UUID(as_uuid=True)（基座同款），实际 {col.type!r}"
            )


@pytest.mark.parametrize("tname", REPAIRED_TABLES)
def test_repaired_table_fk_columns_match_main_orm_style(tname: str) -> None:
    """治理/规则三表逐一断言：DDL 含 UUID 列类型、FK 列类型与主表 kb_facts 同款。"""
    tables = _kb_tables()
    table = tables[tname]
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "UUID" in ddl, f"{tname}: PG DDL 未渲染 UUID 列类型"
    main_fk_types = {type(col.type) for col in _fk_columns(tables["kb_facts"])}
    assert main_fk_types == {PGUUID}, "主表 kb_facts FK 列类型基准异常"
    for col in _fk_columns(table):
        assert type(col.type) in main_fk_types, f"{tname}.{col.name}: FK 列类型与主表不同款: {col.type!r}"
