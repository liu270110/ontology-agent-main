"""仓储接口（04 篇 §4 权威样例签名）：租户作用域构造期绑定（由 `uow.for_tenant()` 创建），方法级不传 tenant_id。

约定（04 §4）：`get` 未命中返回 None；`save` 全量保存聚合标量状态；查询方法按用例定制；
`save`/`append_version` 必须在 UoW 事务内调用；实现归 L6 `repo_impl/ontology_repo.py`。
`append_version` 为发布事务专用 append（制品写成功→PG 版本行，ontology §4/§9 同事务语义），
对齐「只追加实体走专用 append 方法」纪律（Message/TaskEvent 同款）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from services.ontology.domain.model.ontology import Ontology, OntologyStatus, OntologyVersionRef
from services.ontology.domain.model.ontology_read_model import ReadModelProjection


@runtime_checkable
class OntologyRepository(Protocol):
    """ontology 聚合仓储（get/save/list=04 §4 样例签名；append_version=发布事务追加）。"""

    async def get(self, ontology_id: UUID) -> Ontology | None: ...

    async def save(self, ontology: Ontology) -> None:
        """全量保存聚合标量状态（含 active_changeset 行 upsert；head 指针经 current_version_id 落列）。"""
        ...

    async def list(
        self, *, status: OntologyStatus | None = None, offset: int = 0, limit: int = 20
    ) -> list[Ontology]: ...

    async def search(self, query: str, *, limit: int = 20) -> list[Ontology]:
        """本体搜索（api/01 §5.3 search 行）：名称/描述/命名空间 IRI 片段匹配（M2 最小闭环口径）。"""
        ...

    async def append_version(
        self,
        ontology_id: UUID,
        *,
        content: str,
        changelog: str | None = None,
        published_by: UUID | None = None,
    ) -> OntologyVersionRef:
        """发布事务（ontology §9）：制品写成功 → PG 版本行；返回新 head 指针（version/artifact_key/checksum）。"""
        ...

    async def read_artifact(self, artifact_key: str) -> str:
        """读制品内容（权威 TBox 文本；submit 门禁/publish 投影用例取用，ontology §4）。

        制品缺失抛 FileNotFoundError（调用方转 4201 平台码；checksum 三方巡检锚点）。
        """
        ...

    async def replace_read_model(
        self,
        ontology_id: UUID,
        *,
        version: str,
        changeset_id: UUID | None,
        projection: ReadModelProjection,
    ) -> None:
        """发布读模型投影替换写（database/01 §3.3 四表）：同 ontology+version 先删后插。

        必须在调用方 UoW/会话事务内调用（投影与发布版本行同事务=单事务边界，§9）；
        `version` 为版本标识（append_version 返回的 version_ref.version），实现层解析版本行 id。
        """
        ...
