"""L3 类层次公开服务：ontology 读模型（classes 四读模型表）的跨模块轻量查询面。

存在理由：kb 禁入 ontology.data（standards/01 §2.1 规则 3，import-linter 强制），而 kb 检索图路
（LazyGraphRAG lite，docs/OntRAG §4.0）需要 subclass_of 父子类闭包做同类邻接扩展——本模块即
「调 ontology 模块公开服务获取层次」的显式服务调用面（business=规则 3 许可面，调用处注释负责
模块文档引用）。只读、零路由副作用；查询用 text() SQL（不 import 本模块 data/，保证跨模块
import 链不触达 ontology.data——本文件不得新增任何 services.ontology.data import）。

口径：返回租户各本体「当前发布版」（ontologies.current_version_id）的类（iri/name/subclass_of）；
未发布版本（指针为空）自然跳过；层次随读模型投影更新，跨模块消费方自行进程内缓存。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 读模型四表（database/01 §3.3~§3.4）：classes.version_id 对齐 ontologies.current_version_id；
# 本模块所属表，schema 知识留在 ontology 模块内（与 repo_impl 同源口径）。
_CLASS_HIERARCHY_SQL = (
    "SELECT c.iri, c.name, c.subclass_of "
    "FROM classes c "
    "JOIN ontologies o ON o.id = c.ontology_id "
    "JOIN ontology_versions ov ON ov.id = c.version_id "
    "AND (CAST(:ontology_version AS text) IS NULL OR ov.version = :ontology_version) "
    "WHERE o.tenant_id = :tenant_id "
    "AND c.tenant_id = :tenant_id "
    "AND c.version_id = o.current_version_id "
    "AND (CAST(:ontology_id AS uuid) IS NULL OR o.id = CAST(:ontology_id AS uuid)) "
    "ORDER BY c.iri"
)


@dataclass(frozen=True, slots=True)
class ClassHierarchyRow:
    """类层次读模型行（跨模块公开查询返回值，只读快照）。"""

    iri: str
    name: str
    subclass_of: list[str]


async def get_class_hierarchy(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    ontology_id: uuid.UUID | None = None,
    ontology_version: str | None = None,
) -> list[ClassHierarchyRow]:
    """租户类层次（iri/name/subclass_of，当前发布版）；ontology_id 给定时收敛到单本体。

    ontology_version（§5 签名，缺省=当前发布版）：给定即按 versions.version 钉版过滤，
    未发布该版本 → 空结果（消费方闭包退化，非失败）。

    首个消费方 = kb 检索图路的类闭包扩展（LazyGraphRAG lite，docs/OntRAG §4.0）；
    空结果非失败（无已发布本体 → 消费方闭包退化为类自身，见消费处注释）。
    """
    rows = await db.execute(
        text(_CLASS_HIERARCHY_SQL),
        {"tenant_id": tenant_id, "ontology_id": ontology_id, "ontology_version": ontology_version},
    )
    return [
        ClassHierarchyRow(iri=row.iri, name=row.name, subclass_of=list(row.subclass_of or []))
        for row in rows.mappings()
    ]
