"""L3 changeset 用例服务：submit 硬门禁 + publish 读模型投影（任务 2.1 收口，本体核心设计 §6.1/§7）。

两个用例（一文件一用例函数簇，超 500 行拆分）：
- :func:`submit_changeset_for_review` —— 服务端对变更单制品实跑硬门禁（lint+SHACL 自校验），
  结论与证据随单留痕后推进五态机 draft→in_review；客户端 gate_ok/gate_report **仅诊断对照**
  （服务端从不信任自报——宪法3：硬门禁任何治理档位不可跳过，2026-09-27 验收修复）。
- :func:`project_published_version` —— publish 成功后把该版本 TBox 解析投影到 PG 读模型四表
  （classes/properties/axioms/rules，database/01 §3.3），替换式写（同 ontology+version 先删后插），
  与调用方同一会话事务（单事务边界，§9）。

分层：L2 router → 本模块 → {L4 Protocol（OntologyRepository）, L5 ontology_core}；L3 禁 import L6/gateway。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from services.ontology.core import lint, load_turtle, project_tbox
from services.ontology.domain.model.ontology import DomainError, Ontology, OntologyChangeset, OntologyVersionRef
from services.ontology.domain.model.ontology_read_model import ReadModelProjection
from services.ontology.domain.repo.ontology_repo import OntologyRepository

from .ontology_gate import GateReport, run_changeset_gate

_TO_THREAD_TIMEOUT_SECONDS = 30.0


async def submit_changeset_for_review(
    repo: OntologyRepository,
    ontology: Ontology,
    changeset_id: uuid.UUID,
    *,
    turtle: str | None,
    client_gate_ok: bool = False,
    client_gate_report: dict[str, Any] | None = None,
) -> GateReport:
    """submit 前置硬门禁（§6.1 draft→in_review 进入条件=lint+SHACL+一致性全绿）。

    门禁对象：`turtle` 优先；缺省复用 head 版本制品（重提交场景）；两者皆无即 4204 拒绝
    （门禁不可空跑）。服务端结论覆写 changeset.gate_ok/gate_report；客户端自报仅以
    `client_reported` 键随单留痕（审计对照，不参与判定）。
    """
    changeset = _require_active_changeset(ontology, changeset_id)
    content = await _resolve_gate_content(repo, ontology, turtle)
    report = await run_changeset_gate(content)
    changeset.record_gate(
        report.conforms,
        {
            "gate_version": report.gate_version,
            "server_gate": report.model_dump(),
            "client_reported": {  # 仅诊断/审计对照（宪法3：服务端一律不信客户端自报）
                "gate_ok": client_gate_ok,
                "gate_report_keys": sorted((client_gate_report or {}).keys()),
            },
        },
    )
    changeset.submit()  # 4204：门禁未绿拒绝（聚合不变式）
    await repo.save(ontology)
    return report


async def project_published_version(
    repo: OntologyRepository,
    ontology: Ontology,
    *,
    version_ref: OntologyVersionRef,
    changeset: OntologyChangeset,
    routes: dict[str, str] | None = None,
) -> ReadModelProjection:
    """publish 成功后的读模型投影（database/01 §3.3 四表；rollback 新版本行同样适用）。

    `routes` 传门禁（run_changeset_gate）产出的路由判定避免重复 lint；缺省在制品图上重跑
    lint 取权威路由（§2.3：rules.route 初始值由 lint 写入）。替换式写由 L6 实现（先删后插，
    同调用方会话事务）。
    """
    try:
        content = await repo.read_artifact(version_ref.artifact_key)
    except FileNotFoundError as exc:
        raise DomainError(
            f"4201 GATE_ARTIFACT_MISSING: 制品缺失: {version_ref.artifact_key}（checksum 三方巡检应已告警）"
        ) from exc
    graph = await asyncio.wait_for(asyncio.to_thread(load_turtle, content), _TO_THREAD_TIMEOUT_SECONDS)
    if routes is None:  # 门禁路由未传入：制品图重跑 lint（同步 rdflib → to_thread）
        routes = (await asyncio.wait_for(asyncio.to_thread(lint, graph), _TO_THREAD_TIMEOUT_SECONDS)).routes
    projection = await asyncio.wait_for(asyncio.to_thread(project_tbox, graph, routes), _TO_THREAD_TIMEOUT_SECONDS)
    await repo.replace_read_model(
        ontology.id,
        version=version_ref.version,
        changeset_id=changeset.id,
        projection=projection,
    )
    return projection


# ---- 内部装配


def _require_active_changeset(ontology: Ontology, changeset_id: uuid.UUID) -> OntologyChangeset:
    """只允许操作聚合当前变更单（与 L2 `_require_changeset` 同口径；L3 侧兜底 4202）。"""
    changeset = ontology.active_changeset
    if changeset is None or changeset.id != changeset_id:
        raise DomainError("4202 CHANGESET_NOT_ACTIVE: 变更单不存在或不为本体当前变更单（ontology §6.1）")
    return changeset


async def _resolve_gate_content(repo: OntologyRepository, ontology: Ontology, turtle: str | None) -> str:
    """门禁对象解析：body turtle 优先 → head 制品内容（重提交）→ 两者皆无 4204（门禁不可空跑）。"""
    if turtle is not None:
        return turtle
    head = ontology.head_version
    if head is not None:
        try:
            return await repo.read_artifact(head.artifact_key)
        except FileNotFoundError as exc:
            raise DomainError(
                f"4201 GATE_ARTIFACT_MISSING: head 制品缺失: {head.artifact_key}（checksum 三方巡检应已告警）"
            ) from exc
    raise DomainError("4204 GATE_NO_CONTENT: 无可校验制品——首次提交必须携带 turtle（§6.1 门禁不可空跑）")
