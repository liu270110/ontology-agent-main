"""仓储接口（04 篇 §4 权威样例签名）：租户作用域构造期绑定（由 `uow.for_tenant()` 创建），方法级不传 tenant_id。

约定（04 §4）：`get` 未命中返回 None；`save` 全量保存聚合标量状态；查询方法按用例定制；
`save`/`append_version` 必须在 UoW 事务内调用；实现归 L6 `repo_impl/ontology_repo.py`。
`append_version` 为发布事务专用 append（制品写成功→PG 版本行，ontology §4/§9 同事务语义），
对齐「只追加实体走专用 append 方法」纪律（Message/TaskEvent 同款）。
"""

from __future__ import annotations

import builtins
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from services.ontology.domain.model.audit_actions import OntologyAuditAction
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

    async def search(
        self, query: str, *, limit: int = 20
    ) -> builtins.list[Ontology]:  # 类内 list 方法遮蔽内置 list(38 行上方),显式消解
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

    # ---- ONT-1（06 篇 §ONT-1）：审计 / usage 守卫 / 撤除=标记 / 候选拒绝 ----

    async def record_audit(
        self,
        *,
        actor_id: UUID | None,
        action: OntologyAuditAction | str,  # 动作词汇单点（audit_actions）；str 仅留迁移余地
        ontology_id: UUID,
        digest: dict[str, Any],
        trace_id: str | None = None,
        subject: str = "ontology",
        subject_id: UUID | None = None,
    ) -> None:
        """ontology.* 审计行（audit_logs，ONT-1.6 动作族；同事务写入）。"""
        ...

    async def kb_usage_count(self, element_type: str, iri: str) -> int:
        """kb 域对 IRI 的全历史引用计数（ONT-1.4 第 1 档 usage 守卫计数面；不筛 status）。"""
        ...

    async def withdraw_head_element(
        self, ontology_id: UUID, *, version: str, element_type: str, key: str, reason: str
    ) -> int:
        """撤除=标记（ONT-1.3）：head 版本行打 withdrawn 双标记；返回标记行数（0=不存在/已撤）。"""
        ...

    async def decline_candidate(
        self,
        ontology_id: UUID,
        *,
        element_type: str,
        row_id: UUID,
        reason: str,
        actor_id: UUID | None,
        trace_id: str | None = None,
    ) -> bool:
        """候选拒绝（ONT-1.3 防重提层三）：llm_candidate 行打 declined_reason + 审计；False=不可拒。"""
        ...

    async def declined_evidence_floor(self, ontology_id: UUID, *, element_type: str, element_key: str) -> int | None:
        """同形候选再提证据量下限（翻倍判据）；无 declined 历史 → None。"""
        ...
