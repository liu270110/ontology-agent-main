"""能力本体种子（ONT-2.4，06 篇 §ONT-2 契约；docs/ontology/03 §2.4 A 档 21 项）。

两个职责：

1. **纯 builder（常量单源，零 DB 依赖，测试可独立驱动）**——21 行 PG 种子数据全部由代码内
   清单生成，禁止复制粘贴 schema（契约 §ONT-2.4「种子迁移读代码常量生成行，保单源」）：
   - 内核 11：fs5/todo3 走 ToolPort 绑定工厂（build_fs_bindings / build_todo_bindings，schema、
     描述、执行模式、行动 IRI 全取绑定常量）；web2 取 WEB_*_ACTION_IRI + ExtensionMeta（无
     schema 常量，requires/produces 语义推导——侦察批已报风险，上交裁决项）；chat_answer 取
     adapters.base.CHAT_ACTION_IRI（适配器行动，非 ToolPort 工具）。
   - MCP 5：TOOL_SPECS 静态元数据单源（description/annotations），行动 IRI 按内核 MCP 桥
     合成规则（mcp_bridge.MCP_ACTION_IRI_PREFIX + 工具全名，06 §ONT-2.2「出口五枚直挂行动类」），
     requires/produces 取 providers 运行时逐参读点的参数形状（侦察批已报：无常量、两处拼装）。
   - 行动能力 5：解析 power_outage_seed.ttl 行动类（subClassOf ob2:Action 口径，与
     core.projection._is_behavior 同源），执行语义四元取种子 TTL 常量，bindsAction 直挂行动类
     IRI、produces=对应行动类事实（契约 §ONT-2.4 原文）。
   - TBox 对账（:func:`assert_tbox_matches_rows`）：cap.ttl 16 个工具能力个体的
     requires/produces/execution/binds_action/constrainedBy 与本函数产出逐项比对，漂移即拒
     （TTL 是语义登记处而非第二事实源——对账测试同源钉死）。

2. **异步种子链（:func:`seed_capabilities`）**——仿 seed_service.import_seed_as_project 全链：
   装载自证（lint + SHACL 门禁，资产损坏即 4204 拒绝）→ 建「平台能力本体」项目行（nil 租户，
   iri_base=契约定名 http://ontology-agent.local/cap#）→ 单变更单五动词链（solo 档系统自审）→
   append_version（cap.ttl 为 v1 制品）→ publish → 读模型四表投影 → 21 能力行
   （source='seed'，UNIQUE(version_id, iri) 判重幂等）× ONT-1 快照服务同事务落定义快照并回填
   current_definition_version_id。幂等：本体行/版本/能力行/快照四层判重，重跑不重复。

迁移挂接：Alembic 数据迁移（ONT-2 迁移②）为唯一正式调用点——在迁移内以独立 async 引擎驱动
（现存迁移零 services import 的先例被本契约打破，侦察批已上报「迁移编程模型冲突」为待裁决项；
importlinter 豁免边随本批登记 pyproject ignore_imports）。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from rdflib import RDF, RDFS, Graph, Namespace, URIRef
from rdflib.namespace import SH
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.ontology.core.lint import lint
from services.ontology.core.shacl import validate as core_shacl_validate
from services.ontology.core.tbox import OB2, TASK, load_turtle, local_name
from services.ontology.data.orm import Ontology as OntologyORM
from services.ontology.data.orm import OntologyVersion as OntologyVersionORM
from services.ontology.domain.model.ontology import Ontology
from services.platform.kernel import DomainError

# ---------------------------------------------------------------- 常量（契约定名）

CAP_NAMESPACE = "http://ontology-agent.local/cap#"  # 06 篇 §ONT-2.1 契约命名空间
CAP = Namespace(CAP_NAMESPACE)
_CAP_ATOMIC = URIRef(f"{CAP_NAMESPACE}AtomicCapability")
CAP_IRI_BASE = CAP_NAMESPACE
CAP_ONT_NAME = "平台能力本体"
CAP_TTL_PATH = Path(__file__).resolve().parent.parent / "turtle" / "cap.ttl"
CAP_SHAPES_PATH = Path(__file__).resolve().parent.parent / "turtle" / "cap_shapes.ttl"
POWER_SEED_PATH = Path(__file__).resolve().parent.parent / "seeds" / "power_outage_seed.ttl"
# 平台系统租户（NIL 占位，services/mcp/server.py._NIL_TENANT 同源口径）：内建本体随平台版本
# 只读、先于任何租户存在，能力读模型行挂靠其下（跨租户消费投影属 ONT-3 接线批语义）。
SEED_TENANT_ID = uuid.UUID(int=0)
SEED_ACTOR_ID = uuid.UUID(int=0)  # 系统行为者（solo 档自审留痕；audit created_by 恒 NULL 允许）

PWR_NS = "https://ontology-agent.dev/ns/power#"


# ---------------------------------------------------------------- pydantic 白名单结构


class ExecutionSemantics(BaseModel):
    """执行语义四元（capabilities.execution JSONB 白名单形状，06 篇 §ONT-2.2）。"""

    model_config = ConfigDict(extra="forbid")

    execution_mode: str = Field(min_length=1, max_length=32)
    deterministic: bool | None = None
    triggered_by_event: str | None = Field(default=None, max_length=256)
    guarded_by_rule: str | None = Field(default=None, max_length=256)


class CapabilitySeedRow(BaseModel):
    """能力种子行（pydantic 白名单校验入口：requires/produces 逐项为 IRI 字符串）。"""

    model_config = ConfigDict(extra="forbid")

    iri: str = Field(min_length=8, max_length=256, pattern="^https?://")
    kind: str = Field(default="atomic", pattern="^(atomic|composite)$")
    name: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=256)
    description: str | None = None
    requires: list[str] = Field(default_factory=list, min_length=1)  # AtomicCapabilityShape：各≥1
    produces: list[str] = Field(default_factory=list, min_length=1)
    constrained_by: str | None = Field(default=None, max_length=256)
    execution: ExecutionSemantics
    binds_action: str | None = Field(default=None, max_length=256)

    def definition_fields(self) -> dict[str, Any]:
        """ONT-1.2 capability 定义字段投影（白名单五项，06 §ONT-2.2 原文口径）。"""
        return {
            "requires": self.requires,
            "produces": self.produces,
            "constrained_by": self.constrained_by,
            "execution": self.execution.model_dump(),
            "binds_action": self.binds_action,
        }


# ---------------------------------------------------------------- TBox 装载与门禁自证


def load_cap_graph(path: Path = CAP_TTL_PATH) -> Graph:
    """cap.ttl → rdflib 图（解析失败统一 ValueError，tbox 同款）。"""
    try:
        return load_turtle(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cap.ttl 不可读（{path}）：{exc}") from exc


def load_cap_shapes_graph(path: Path = CAP_SHAPES_PATH) -> Graph:
    """cap_shapes.ttl → rdflib 图（同款）。"""
    try:
        return load_turtle(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cap_shapes.ttl 不可读（{path}）：{exc}") from exc


def cap_tbox_gate(cap_graph: Graph | None = None, shapes_graph: Graph | None = None) -> CapTBoxGate:
    """cap TBox 自证门禁：lint 全绿 + SHACL **Violation 级清零**（资产损坏即拒，seed_service 同款）。

    判定口径注记（实测沉淀）：pySHACL 的 conforms 把 Warning 级结果一并计入 False
    （shacl.py 封装原样透传），而契约把执行语义四元齐备定为 warn 级——工具能力缺
    triggeredByEvent/guardedByRule 是常态化告警。故门禁判据=无 Violation 级结果
    （warn 仅计数留痕），与「校验失败即拒绝」的 Violation 语义一致。
    """
    cap_graph = cap_graph if cap_graph is not None else load_cap_graph()
    shapes_graph = shapes_graph if shapes_graph is not None else load_cap_shapes_graph()
    lint_report = lint(cap_graph)
    shacl_report = core_shacl_validate(cap_graph, shapes_graph, tbox_graph=cap_graph)
    violations = [r for r in shacl_report.results if r.severity == str(SH.Violation)]
    warnings = [r for r in shacl_report.results if r.severity == str(SH.Warning)]
    return CapTBoxGate(
        lint_ok=lint_report.ok,
        lint_violations=[f"{v.code}: {v.message}" for v in lint_report.violations],
        shacl_violation_count=len(violations),
        shacl_warning_count=len(warnings),
        violations=[str(v.message) for v in violations],
    )


class CapTBoxGate(BaseModel):
    """cap TBox 自证报告（lint + SHACL 分级计数；violation_free=门禁判据）。"""

    lint_ok: bool
    lint_violations: list[str] = []
    shacl_violation_count: int
    shacl_warning_count: int
    violations: list[str] = []

    @property
    def violation_free(self) -> bool:
        return self.lint_ok and self.shacl_violation_count == 0

    def ensure_ok(self) -> None:
        """门禁未绿即 4204 拒绝（资产损坏口径，seed_service 同错误码族）。"""
        if not self.violation_free:
            raise DomainError(
                f"4204 CAP_TBOX_GATE_FAILED: cap TBox 自证未过门禁，拒绝种子："
                f"lint={self.lint_violations} shacl_violations={self.violations}"
            )


# ---------------------------------------------------------------- 16 工具能力（代码常量单源）

# 签名/形状/确定性语义表：requires/produces=域值域签名（schema/运行时参数形状推导），
# shape=cap_shapes.ttl 形状本地名，deterministic=None 表示取显式常量（MCP idempotentHint），
# mode=None 表示取绑定 ExecutionMode；手写值均为「无代码常量项」的语义推导（侦察批风险已报）。
_TOOL_SIGNATURES: dict[str, dict[str, Any]] = {
    "fs.read": {"requires": ("WorkspaceFile",), "produces": ("FileText",), "shape": "FsReadShape", "det": True},
    "fs.write": {"requires": ("WorkspaceFile",), "produces": ("WorkspaceFile",), "shape": "FsWriteShape", "det": False},
    "fs.edit": {"requires": ("WorkspaceFile",), "produces": ("WorkspaceFile",), "shape": "FsEditShape", "det": False},
    "fs.glob": {"requires": ("Workspace",), "produces": ("GlobMatch",), "shape": "FsGlobShape", "det": True},
    "fs.grep": {"requires": ("Workspace",), "produces": ("GrepMatch",), "shape": "FsGrepShape", "det": True},
    "web.fetch": {
        "requires": ("Url",), "produces": ("WebContent",), "shape": "WebFetchShape", "det": False,
        "mode": "read",  # 无 ExecutionMode 常量：egress 只读语义
        "required_args": ("url",),  # fetch.py 运行时直读 call.parameters.get("url") 缺失即拒
        "description": "白名单出口抓取网页（egress 门禁 + 截断/spill）。",
    },
    "web.search": {
        "requires": ("SearchQuery",), "produces": ("SearchHits",), "shape": "WebSearchShape", "det": False,
        "mode": "read", "required_args": ("query",),  # search.py 运行时缺 query 即拒
        "description": "检索引擎适配（结果按白名单过滤）。",
    },
    "todo.write": {
        "requires": ("TodoDeclaration",), "produces": ("TodoBoard",), "shape": "TodoWriteShape", "det": False
    },
    "todo.update": {
        "requires": ("TodoDeclaration",), "produces": ("TodoBoard",), "shape": "TodoUpdateShape", "det": False
    },
    "todo.read": {"requires": ("TodoBoard",), "produces": ("TodoBoard",), "shape": "TodoReadShape", "det": True},
    "chat_answer": {
        "requires": ("UserMessage",), "produces": ("ChatAnswer",), "shape": "ChatAnswerShape", "det": False,
        "mode": "write", "required_args": ("message",),  # 适配器行动：ChatTurn.message 必填语义
        "description": "会话回答适配器行动（非 ToolPort 工具；LLM 生成非确定）。",
    },
    # MCP 5：description 取 TOOL_SPECS；execution_mode/deterministic 取 annotations
    # （readOnlyHint→mode、idempotentHint→det）；required_args 取 providers 运行时逐参读点形状。
    "knowledge.search": {
        "requires": ("KnowledgeQuery",), "produces": ("KnowledgeEvidence",), "shape": "KnowledgeSearchShape",
        "det": None, "mode": None, "required_args": ("query",),
    },
    "ontology.validate": {
        "requires": ("OntologyGraph",), "produces": ("ShaclReport",), "shape": "OntologyValidateShape",
        "det": None, "mode": None, "required_args": ("ontology_id", "graph"),
    },
    "ontology.query": {
        "requires": ("SparqlQuery", "OntologyGraph"), "produces": ("SparqlResult",), "shape": "OntologyQueryShape",
        "det": None, "mode": None, "required_args": ("ontology_id", "sparql"),
    },
    "memory.invalidate": {
        "requires": ("MemoryFact",), "produces": ("MemoryTombstone",), "shape": "MemoryInvalidateShape",
        "det": None, "mode": None, "required_args": ("fact_id", "reason"),
    },
    "writeback.status": {
        "requires": ("LedgerQuery",), "produces": ("LedgerStatus",), "shape": "WritebackStatusShape",
        "det": None, "mode": None, "required_args": (),  # 三键 anyOf（R2 封闭集外），无单参必需
    },
}

_TOOL_ORDER: tuple[str, ...] = (
    "fs.read",
    "fs.write",
    "fs.edit",
    "fs.glob",
    "fs.grep",
    "web.fetch",
    "web.search",
    "todo.write",
    "todo.update",
    "todo.read",
    "chat_answer",
    "knowledge.search",
    "ontology.validate",
    "ontology.query",
    "memory.invalidate",
    "writeback.status",
)


def _binding_catalog() -> dict[str, dict[str, Any]]:
    """绑定工厂一次性装配（无 I/O；todo 工厂以 nil 租户账本注入，仅取元数据不执行）。"""
    from services.agent.business.adapters.base import CHAT_ACTION_IRI
    from services.agent.business.capabilities.fs.bindings import build_fs_bindings
    from services.agent.business.capabilities.mcp_bridge import MCP_ACTION_IRI_PREFIX
    from services.agent.business.capabilities.todo.bindings import build_todo_bindings
    from services.agent.business.capabilities.web.bindings import build_web_bindings
    from services.agent.business.kernel.ledger import KernelLedger
    from services.mcp.server import TOOL_SPECS

    fs = {b.meta.name: b for b in build_fs_bindings()}
    todo = {
        b.meta.name: b
        for b in build_todo_bindings(
            criteria=(), ledger=KernelLedger(tenant_id=SEED_TENANT_ID, trace_id="ont2-seed")
        )
    }
    fetch, search = build_web_bindings(fetch_allowlist=())
    bindings: dict[str, dict[str, Any]] = {}
    for name, binding in {**fs, **todo}.items():
        bindings[name] = {
            "name": binding.meta.name,
            "description": binding.description,
            "schema": binding.input_schema,
            "mode": binding.execution_mode.value,
            "action_iri": binding.meta.semantic_annotation["action_iri"],
        }
    for tool in (fetch, search):
        name = tool.meta.name
        bindings[name] = {
            "name": name,
            "description": _TOOL_SIGNATURES[name]["description"],
            "schema": None,  # 无 schema 常量（侦察批风险：web 工具直读 call.parameters）
            "mode": _TOOL_SIGNATURES[name]["mode"],
            "action_iri": tool.meta.semantic_annotation["action_iri"],
        }
    bindings["chat_answer"] = {
        "name": "chat_answer",
        "description": _TOOL_SIGNATURES["chat_answer"]["description"],
        "schema": None,  # 适配器行动，无参数形状常量
        "mode": _TOOL_SIGNATURES["chat_answer"]["mode"],
        "action_iri": CHAT_ACTION_IRI,
    }
    for name in ("knowledge.search", "ontology.validate", "ontology.query", "memory.invalidate", "writeback.status"):
        description, annotations, _meta = TOOL_SPECS[name]
        bindings[name] = {
            "name": name,
            "description": description,
            "schema": None,  # 入参形状散在 providers 运行时读点（侦察批风险：两处拼装）
            "mode": "read" if annotations.get("readOnlyHint") else "write",
            "det_override": True if annotations.get("idempotentHint") else False,
            "action_iri": f"{MCP_ACTION_IRI_PREFIX}{name}",  # MCP 桥行动 IRI 合成规则（mcp_bridge.py:52）
        }
    return bindings


def _binding_required_args(schema: dict[str, Any] | None, name: str) -> tuple[str, ...]:
    """必需参数集（形状对账锚点）：有 schema 常量取 required，否则取语义表推导值。"""
    if schema is not None:
        return tuple(schema.get("required", ()))
    override = _TOOL_SIGNATURES[name].get("required_args")
    if override is None:
        raise ValueError(f"{name} 既无 schema 常量也无 required_args 推导表")
    return tuple(override)


def build_tool_seed_rows() -> list[CapabilitySeedRow]:
    """16 工具能力行（内核 11 + MCP 5；代码常量单源，禁止复制粘贴 schema）。"""
    catalog = _binding_catalog()
    rows: list[CapabilitySeedRow] = []
    for name in _TOOL_ORDER:
        spec = _TOOL_SIGNATURES[name]
        binding = catalog[name]
        det = binding.get("det_override")
        if det is None:
            det = spec["det"]
        mode = binding["mode"] if spec.get("mode") is None else spec["mode"]
        rows.append(
            CapabilitySeedRow(
                iri=f"{CAP_NAMESPACE}{name.replace('.', '-').replace('_', '-')}",
                name=binding["name"],
                description=binding["description"],
                requires=[f"{CAP_NAMESPACE}{c}" for c in spec["requires"]],
                produces=[f"{CAP_NAMESPACE}{c}" for c in spec["produces"]],
                constrained_by=f"{CAP_NAMESPACE}{spec['shape']}",
                execution=ExecutionSemantics(execution_mode=mode, deterministic=bool(det)),
                binds_action=binding["action_iri"],
            )
        )
    return rows


# ---------------------------------------------------------------- 5 业务行动能力（种子 TTL 单源）

# requires 域签名（作用对象类，power 种子词表；语义推导，TTL 未声明 requires——行动类闭环
# 只约束事件/守卫两元，域签名属能力侧增补，推导式在此留档）：
_ACTION_REQUIRES: dict[str, tuple[str, ...]] = {
    "QueryTicket": ("WorkTicket",),
    "LocateFault": ("OutageEvent",),
    "DispatchRepair": ("WorkTicket", "RepairCrew"),
    "ConfirmRestoration": ("OutageEvent",),
    "NotifyCustomer": ("WorkTicket", "Customer"),
}


def build_action_seed_rows(seed_graph: Graph | None = None) -> list[CapabilitySeedRow]:
    """5 业务行动能力行：解析 power_outage_seed.ttl 行动类（subClassOf ob2:Action 口径）。

    执行语义四元取种子 TTL 常量（task:executionMode/task:deterministic/ob2:triggeredByEvent/
    ob2:guardedByRule）；bindsAction 直挂行动类 IRI；produces=对应行动类事实（契约 §ONT-2.4
    原文）；constrained_by=守卫规则 shape（种子 sh:NodeShape，合法 shape 引用）。
    """
    graph = seed_graph if seed_graph is not None else load_turtle(POWER_SEED_PATH.read_text(encoding="utf-8"))
    rows: list[CapabilitySeedRow] = []
    for cls in sorted(graph.subjects(RDFS.subClassOf, OB2.Action), key=str):
        action_iri = str(cls)
        local = local_name(action_iri)
        mode = graph.value(cls, TASK.executionMode)
        det = graph.value(cls, TASK.deterministic)
        event = graph.value(cls, OB2.triggeredByEvent)
        guard = graph.value(cls, OB2.guardedByRule)
        if None in (mode, det, event, guard):
            raise DomainError(f"4204 ACTION_SEED_INCOMPLETE: 行动类 {local} 缺执行语义四元（种子资产损坏）")
        requires = _ACTION_REQUIRES.get(local)
        if requires is None:
            raise DomainError(f"4204 ACTION_SEED_UNKNOWN: 种子行动类 {local} 不在 A 档 5 项清单内")
        label = graph.value(cls, RDFS.label)
        rows.append(
            CapabilitySeedRow(
                iri=f"{CAP_NAMESPACE}{_kebab(local)}",
                name=local,
                label=str(label) if label is not None else None,
                description=f"业务行动能力：{label}（实现行动类 {local}，效果=行动类事实提议）",
                requires=[f"{PWR_NS}{c}" for c in requires],
                produces=[action_iri],
                constrained_by=str(guard),
                execution=ExecutionSemantics(
                    execution_mode=local_name(str(mode)),
                    deterministic=bool(det.toPython()),
                    triggered_by_event=str(event),
                    guarded_by_rule=str(guard),
                ),
                binds_action=action_iri,
            )
        )
    return rows


def build_seed_rows() -> list[CapabilitySeedRow]:
    """A 档 21 项全量（16 工具 + 5 行动；排序=工具序 + 行动 IRI 序，输出确定性）。"""
    rows = [*build_tool_seed_rows(), *build_action_seed_rows()]
    if len(rows) != 21:  # 计数门禁走 DomainError（不用裸 assert：-O 下被剥除即失效）
        raise DomainError(
            f"4204 CAP_SEED_COUNT_DRIFT: A 档清单漂移：期望 21 项，得 {len(rows)}（06 §ONT-2.4 / 03 §2.4）"
        )
    return rows


def _kebab(camel: str) -> str:
    return "".join(("-" + ch.lower()) if ch.isupper() else ch for ch in camel).lstrip("-")


# ---------------------------------------------------------------- TBox ↔ 常量对账


def tbox_individuals(cap_graph: Graph) -> dict[str, dict[str, Any]]:
    """cap.ttl 工具能力个体 → {本地名: 签名面}（对账取材；ONT-3 投影链接线可复用）。"""
    individuals: dict[str, dict[str, Any]] = {}
    for term in cap_graph.subjects(RDF.type, _CAP_ATOMIC):
        local = local_name(str(term))
        individuals[local] = {
            "requires": sorted(str(o) for o in cap_graph.objects(term, CAP.requires)),
            "produces": sorted(str(o) for o in cap_graph.objects(term, CAP.produces)),
            "constrained_by": sorted(str(o) for o in cap_graph.objects(term, CAP.constrainedBy)),
            "execution_mode": sorted(str(o) for o in cap_graph.objects(term, CAP.executionMode)),
            "deterministic": sorted(str(o).lower() for o in cap_graph.objects(term, CAP.deterministic)),
            "binds_action": sorted(str(o) for o in cap_graph.objects(term, CAP.bindsAction)),
        }
    return individuals


def assert_tbox_matches_rows(rows: list[CapabilitySeedRow], cap_graph: Graph | None = None) -> None:
    """TBox 个体 ↔ 常量行 对账（fail-closed 防双向漂移）：16 工具能力逐项相等，行动能力不入 TTL。"""
    graph = cap_graph if cap_graph is not None else load_cap_graph()
    individuals = tbox_individuals(graph)
    problems: list[str] = []
    for row in rows:
        local = row.iri.removeprefix(CAP_NAMESPACE)
        if row.binds_action and row.binds_action.startswith(PWR_NS):
            continue  # 行动能力个体不落 cap.ttl（租户侧数据，见模块 docstring）
        ind = individuals.get(local)
        if ind is None:
            problems.append(f"{local}: cap.ttl 缺个体")
            continue
        if ind["requires"] != sorted(row.requires):
            problems.append(f"{local}: requires 漂移 {ind['requires']} != {sorted(row.requires)}")
        if ind["produces"] != sorted(row.produces):
            problems.append(f"{local}: produces 漂移 {ind['produces']} != {sorted(row.produces)}")
        if ind["constrained_by"] != [row.constrained_by]:
            problems.append(f"{local}: constrainedBy 漂移 {ind['constrained_by']} != [{row.constrained_by}]")
        if ind["execution_mode"] != [row.execution.execution_mode]:
            problems.append(f"{local}: executionMode 漂移")
        if ind["deterministic"] != [str(row.execution.deterministic).lower()]:
            problems.append(f"{local}: deterministic 漂移")
        if ind["binds_action"] != ([row.binds_action] if row.binds_action else []):
            problems.append(f"{local}: bindsAction 漂移")
    extra = set(individuals) - {r.iri.removeprefix(CAP_NAMESPACE) for r in rows}
    if extra:
        problems.append(f"cap.ttl 多出个体: {sorted(extra)}")
    if problems:
        raise DomainError(f"4204 CAP_TBOX_DRIFT: cap.ttl 与代码常量行对账失败（双向防漂移）：{problems}")


def assert_shapes_match_rows(rows: list[CapabilitySeedRow], shapes_graph: Graph | None = None) -> None:
    """参数形状对账：cap_shapes.ttl 必需参数集（minCount 1 的 sh:path）== 代码 schema required 集。"""
    shapes = shapes_graph if shapes_graph is not None else load_cap_shapes_graph()
    catalog = _binding_catalog()
    problems: list[str] = []
    for row in rows:
        if row.constrained_by is None or not row.constrained_by.startswith(CAP_NAMESPACE):
            continue  # 行动能力守卫形状在 power 种子侧，不入本对账
        local = row.constrained_by.removeprefix(CAP_NAMESPACE)
        term = URIRef(row.constrained_by)
        if (term, RDF.type, SH.NodeShape) not in shapes:
            problems.append(f"{local}: cap_shapes.ttl 缺形状声明")
            continue
        declared: set[str] = set()
        for prop_shape in shapes.objects(term, SH.property):
            path = shapes.value(prop_shape, SH.path)
            min_count = shapes.value(prop_shape, SH.minCount)
            if path is not None and min_count is not None and int(min_count.toPython()) >= 1:
                arg_local = str(path).rsplit("#", 1)[-1]
                declared.add(arg_local.removeprefix("arg-"))  # 参数名 = cap:arg-<name> 的 <name> 段
        expected = set(_binding_required_args(catalog[row.name]["schema"], row.name))
        if declared != expected:
            problems.append(f"{local}: 必需参数集漂移 {sorted(declared)} != {sorted(expected)}")
    if problems:
        raise DomainError(f"4204 CAP_SHAPE_DRIFT: cap_shapes.ttl 参数形状与代码 schema 对账失败：{problems}")


# ---------------------------------------------------------------- 异步种子链（Alembic 数据迁移正式入口）


class CapSeedReport(BaseModel):
    """种子结果报告（迁移日志/测试断言面）：挂靠标识 + 四层幂等计数。"""

    ontology_id: uuid.UUID
    iri_base: str
    version: str
    checksum: str
    rows_total: int
    rows_inserted: int
    rows_skipped: int
    snapshots_taken: int
    version_created: bool


async def seed_capabilities(
    db: AsyncSession,
    *,
    artifacts: Any | None = None,
    tenant_id: uuid.UUID = SEED_TENANT_ID,
) -> CapSeedReport:
    """cap TBox → 平台能力本体项目 + 21 能力行（调用方会话=单事务边界，seed_service 同款）。

    幂等四层：①本体行按 (tenant, iri_base) 判重；②版本按制品 checksum 判重（同内容不追版本）；
    ③能力行按 UNIQUE(version_id, iri) 判重；④快照按 definition_hash 判等（ONT-1 机制自带）。
    """
    # 跨模块 FK 解析前置：ontologies.owner_business FK→users（iam）——ORM flush 的 sort_tables
    # 需要 users 表在 Base.metadata 内。迁移子进程只加载了本模块链（无 registry 聚合），首次
    # `alembic upgrade head` 即崩 NoReferencedTableError（全量套件实测）；函数级导入聚合注册表
    # （幂等，已加载时零开销），同时覆盖直接调用与迁移壳两条入口。
    from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（跨模块 FK 解析）

    # 0) 装载自证门禁（lint + SHACL Violation 清零 + TBox/形状与常量行对账——资产损坏/漂移即拒）
    gate = cap_tbox_gate()
    gate.ensure_ok()
    rows = build_seed_rows()
    assert_tbox_matches_rows(rows)
    assert_shapes_match_rows(rows)
    turtle = CAP_TTL_PATH.read_text(encoding="utf-8")
    checksum = hashlib.sha256(turtle.encode("utf-8")).hexdigest()  # 制品哈希口径=LocalArtifactStore.put

    from services.ontology.business.changeset_service import project_published_version
    from services.ontology.data.repo_impl.capability_repo import CapabilityRepository
    from services.ontology.data.repo_impl.element_version_repo import OntologyElementVersionRepo
    from services.ontology.data.repo_impl.ontology_repo import LocalArtifactStore, PgOntologyRepository

    repo = PgOntologyRepository(db, tenant_id, artifacts=artifacts or LocalArtifactStore())

    # ① 本体行（幂等）
    existing = (
        await db.execute(
            select(OntologyORM).where(OntologyORM.tenant_id == tenant_id, OntologyORM.iri_base == CAP_IRI_BASE)
        )
    ).scalar_one_or_none()
    ontology = await repo.get(existing.id) if existing is not None else None
    if ontology is None:
        ontology = Ontology(tenant_id=tenant_id, iri_base=CAP_IRI_BASE, name=CAP_ONT_NAME)
        await repo.save(ontology)

    # ② 版本（checksum 判重；变更单五动词链——系统自审零用户 FK 写：nil 系统租户无 users 行，
    # 审批留痕以签名序列走 publish 的 approvals 传参覆盖（聚合「缺省复用 approve 留痕」的
    # 显式传参路径，_assert_approvals 逐签回放照常生效），applicant_id/reviewer_id/
    # published_by 恒 NULL 不写 users FK 列）
    version_created = False
    version_ref = ontology.head_version
    if version_ref is None or version_ref.checksum != checksum:
        changeset = ontology.open_changeset("能力本体种子（ONT-2 A 档 21 项）")
        changeset.record_gate(True, {"source": "platform_asset", "gate": gate.model_dump()})
        changeset.submit()
        version_ref = await repo.append_version(
            ontology.id, content=turtle, changelog="cap TBox v1（A 档 21 能力）", published_by=None
        )
        ontology.publish(
            True,
            {
                "approver_id": str(SEED_ACTOR_ID),
                "note": "能力本体种子：系统自审（内建资产随平台版本只读，nil 租户无 users 行）",
                "signatures": [{"approver_id": str(SEED_ACTOR_ID), "tier": "solo"}],
            },
            version_ref=version_ref,
            actor_id=None,
        )
        await repo.save(ontology)
        await project_published_version(repo, ontology, version_ref=version_ref, changeset=changeset)
        version_created = True

    version_id = (
        await db.execute(
            select(OntologyVersionORM.id).where(
                OntologyVersionORM.ontology_id == ontology.id,
                OntologyVersionORM.tenant_id == tenant_id,
                OntologyVersionORM.version == version_ref.version,
            )
        )
    ).scalar_one_or_none()
    if version_id is None:
        raise DomainError("4201 CAP_VERSION_MISSING: 版本行不存在（append_version 同事务前置失败）")

    # ③④ 能力行 × 定义快照（同事务；UNIQUE(version_id, iri) 判重 + 快照 hash 判等）
    caps = CapabilityRepository(db, tenant_id)
    snapshots = OntologyElementVersionRepo(db, tenant_id)
    inserted = skipped = snap_count = 0
    for row in rows:
        if await caps.find_by_iri(ontology.id, version_id, row.iri) is not None:
            skipped += 1
            continue
        snapshot = await snapshots.snapshot(
            ontology.id, "capability", row.iri, row.definition_fields(), created_by=None
        )
        snap_count += 1
        await caps.insert_seed_row(
            ontology.id,
            version_id,
            iri=row.iri,
            kind=row.kind,
            name=row.name,
            label=row.label,
            description=row.description,
            requires=row.requires,
            produces=row.produces,
            constrained_by=row.constrained_by,
            execution=row.execution.model_dump(),
            binds_action=row.binds_action,
            definition_version_id=snapshot.id,
        )
        inserted += 1
    return CapSeedReport(
        ontology_id=ontology.id,
        iri_base=CAP_IRI_BASE,
        version=version_ref.version,
        checksum=checksum,
        rows_total=len(rows),
        rows_inserted=inserted,
        rows_skipped=skipped,
        snapshots_taken=snap_count,
        version_created=version_created,
    )


def run_seed(dsn: str | None = None) -> CapSeedReport:
    """迁移壳：独立 async 引擎 + 单事务驱动种子链（同步 Alembic 环境 → asyncio.run 桥）。

    dsn 缺省取统一配置层（Settings.pg_dsn）；测试以一次性库 DSN 注入即得端到端覆盖。
    """
    if sys.platform == "win32":  # psycopg 异步要求 Selector 循环（tests/ontology 现役纪律同源）
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    from services.platform.config import get_settings

    async def _main() -> CapSeedReport:
        engine = create_async_engine(dsn or get_settings().pg_dsn)
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session, session.begin():
                return await seed_capabilities(session)
        finally:
            await engine.dispose()

    return asyncio.run(_main())
