"""L3 变更单硬门禁（宪法3：LLM 候选非成品 + 硬门禁任何治理档位不可跳过；本体核心设计 §6.1）。

服务端对 pending changeset 的 Turtle 制品**实跑** lint（R1 公理封闭集/R2 SHACL 算子/R3 ECA 判定表）
+ SHACL 自校验（制品既作 shapes 又作 data：shape 声明内部一致性 + 制品内实例合规），
产出 GateReport（conforms / violations / gate 版本号）。

客户端 `gate_ok` / `gate_report` 一律**不参与判定**（服务端从不信任自报，2026-09-27 验收修复），
仅作诊断/审计对照随单留痕（见 changeset_service.submit_changeset_for_review）。

分层：L2 router → 本模块（L3）→ {L4 Protocol, L5 ontology_core}；L3 禁 import L6/gateway。
lint/pySHACL 为同步 rdflib 栈——统一 `asyncio.to_thread` 包裹并设超时（standards/01 异步纪律）。
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from services.ontology.core import lint, load_turtle, validate
from services.ontology.domain.model.ontology import DomainError

# 门禁版本号：lint 判定表（本体核心设计 §2.3）+ R2 算子封闭集快照版本，随 GateReport/变更单留痕
GATE_VERSION = "gate.v1"
_GATE_TIMEOUT_SECONDS = 30.0  # SLA 目标 1k 三元组 P95≤500ms；30s 超时视为门禁失败（fail-closed）


class GateViolation(BaseModel):
    """单条门禁违规（结果可追溯，04 篇 §5）：source/path 携带原文指针。"""

    stage: str = Field(pattern="^(parse|lint|shacl)$")  # 违规阶段
    code: str | None = None  # lint 违规码 / SHACL 约束组件 IRI
    message: str | None = None
    source: str | None = None  # lint=规则/术语 IRI；shacl=source_shape；parse=制品
    path: str | None = None  # lint=违规码定位；shacl=resultPath/focusNode


class GateReport(BaseModel):
    """门禁报告（conforms=总结论：lint 无违规 且 SHACL 自校验 conform）。"""

    conforms: bool
    gate_version: str = GATE_VERSION
    profile: str = "rl"
    lint_ok: bool = True
    shacl_conforms: bool = True
    violations: list[GateViolation] = Field(default_factory=list)
    routes: dict[str, str] = Field(default_factory=dict)  # 规则 IRI → 判定路由（publish 投影复用，§2.3）


async def run_changeset_gate(turtle_content: str) -> GateReport:
    """服务端硬门禁：Turtle 解析 → lint 三路由判定 → SHACL 自校验（任一失败即 conforms=False）。

    解析失败/超时不抛网络类异常：返回结构化违规（fail-closed，门禁语义=失败即拒绝，§5.2）；
    仅线程池耗尽等基础设施故障向上抛（asyncio.TimeoutError 已转门禁违规）。
    """
    try:
        graph = await asyncio.wait_for(asyncio.to_thread(load_turtle, turtle_content), _GATE_TIMEOUT_SECONDS)
    except ValueError as exc:  # load_turtle 契约：Turtle 解析失败转 ValueError
        return GateReport(
            conforms=False,
            lint_ok=False,
            shacl_conforms=False,
            violations=[
                GateViolation(
                    stage="parse", code="GATE_PARSE_FAILED", message=str(exc), source="<artifact>", path="turtle"
                )
            ],
        )
    except TimeoutError as exc:
        raise DomainError(
            f"4204 GATE_TIMEOUT: 门禁执行超时（>{_GATE_TIMEOUT_SECONDS:.0f}s，fail-closed 拒绝，§6.1）"
        ) from exc

    # 以下两步均为同步 rdflib/pySHACL 计算：to_thread 包裹避免阻塞事件循环（standards/01 异步纪律）
    try:
        lint_report = await asyncio.wait_for(asyncio.to_thread(lint, graph), _GATE_TIMEOUT_SECONDS)
        # 自校验：制品既作 shapes 又作 data——shape 声明内部一致性 + 制品内实例合规（ontology §5.2）
        shacl_report = await asyncio.wait_for(asyncio.to_thread(validate, graph, graph), _GATE_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise DomainError(
            f"4204 GATE_TIMEOUT: 门禁执行超时（>{_GATE_TIMEOUT_SECONDS:.0f}s，fail-closed 拒绝，§6.1）"
        ) from exc

    violations = [
        GateViolation(stage="lint", code=v.code, message=v.message, source=v.subject, path=v.code)
        for v in lint_report.violations
    ]
    violations.extend(
        GateViolation(
            stage="shacl",
            code=r.constraint,
            message=r.message,
            source=r.source_shape or r.constraint,
            path=r.path or r.focus_node,
        )
        for r in shacl_report.results
    )
    return GateReport(
        conforms=lint_report.ok and shacl_report.conforms,
        profile=lint_report.profile,
        lint_ok=lint_report.ok,
        shacl_conforms=shacl_report.conforms,
        violations=violations,
        routes=dict(lint_report.routes),
    )
