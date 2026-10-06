"""G0 缺口轨核心：缺口事件采集 + 场景指纹聚类 + 达标开单（architecture/09 §13.3 G0）。

G0 是工具进化全链路（§13.3 G0→G1→G2→G3）的信号源，也是本体缺口轨（§13.4 第三轨）的
承载：**全确定性、零 LLM**（§13.1 分工铁律——缺口检测属确定性环节，LLM 起草是 G1 后续
批次，本模块不做也不 stub）。行动缺口事件四型（§13.3 G0 逐字）：

- ``unmapped_intent``：任务无法归类到任何行动类（内核任务归类线上报，v1 只定义采集接口）；
- ``unbound_action``：行动类存在但无实现绑定（dispatcher resolve-miss 真实接线，sinks.py）；
- ``execution_failure``：绑定工具失败/超时/结果校验不过（回写台账 FAILED 行真实接线，sinks.py）；
- ``manual_fallback``：人工接管（任务升级人工事件线上报，v1 只定义采集接口）。

**场景指纹口径 v1**（``scenario_fingerprint``）：``sha256(版本 + kind + 规范化主键 + 排序去重
实体类型集 + 规范化失败模式)``，即 §13.3「意图向量 + 实体类型集 + 失败模式」的规范化拼接——
主键=意图向量（``intent_key`` 规范化：NFKC→casefold→去首尾空白→连续空白折叠）或行动类 IRI
（``action_iri`` 仅去首尾空白：IRI 大小写敏感不做折叠）；失败模式=错误码段规范化（取首个
冒号前段并 casefold，如 ``"MCP_TARGET_UNAVAILABLE: x"``→``"mcp_target_unavailable"``）。
版本串 ``v1`` 参与哈希，口径升级即换指纹空间，新旧簇互不混算。

聚类与开单（§13.3 G0 达标线逐字）：滑窗（默认 30 天）内同指纹计数 ≥ 阈值（默认 5 次）即开
缺口工单——经 ``RsiService.submit`` 入候选池（status=draft、surface=O1、evidence=簇证据），
同一指纹已有 open draft 工单则不重复开（去重）；候选生命周期后续迁移必经
``Proposal.transition``（宪法：受控状态迁移，本模块不做任何迁移）。

持久化边界：``JsonlGapStore`` 追加写 JSONL 落盘（events 与 proposals 两文件，目录可注入）——
**PG rsi_proposals 表随改表流程批次（06 契约 → database/01 DDL → Alembic 迁移），v1 文件态
可审计可复现**。聚类按（租户, 指纹）分组：指纹是场景同一性，不混算跨租户。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

from services.rsi.proposal import Proposal, ProposalStatus, TriggerTrack
from services.rsi.service import RsiService
from services.rsi.surfaces import EvolutionSurface
from services.rsi.whitelist import ImprovementType

# 场景指纹口径版本（参与哈希输入；升级口径即换版本号，新旧指纹空间隔离）
GAP_FINGERPRINT_VERSION = "v1"

# 缺口工单目标载体前缀（白名单第 ⑤ 类 tool_description 收编进 O1 面，§13.6：
# "工具描述类改进（§3-5）收编进 O1 面（描述与实现同属行动类绑定层的进化产物）"）
GAP_TARGET_PREFIX = "tool_candidates"

# 开单证据抽样上限（样本进 evidence 供 G1/G2 复核，不随簇规模线性膨胀）
DEFAULT_SAMPLE_SIZE = 3

_T = TypeVar("_T")


class GapKind(StrEnum):
    """行动缺口事件四型（§13.3 G0 逐字定稿）。"""

    UNMAPPED_INTENT = "unmapped_intent"  # 任务无法归类到任何行动类
    UNBOUND_ACTION = "unbound_action"  # 行动类存在但无实现绑定
    EXECUTION_FAILURE = "execution_failure"  # 绑定工具失败/超时/结果校验不过
    MANUAL_FALLBACK = "manual_fallback"  # 人工接管


def _norm_key(text: str) -> str:
    """主键规范化 v1：NFKC → casefold → 去首尾空白 → 连续空白折叠为单空格。"""
    collapsed = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return collapsed


def norm_failure_mode(raw: str) -> str:
    """失败模式规范化 v1：取首个冒号前的错误码段并 casefold（无冒号取全文）。

    台账 ``last_error`` 形如 ``"MCP_TARGET_UNAVAILABLE: 行动类未绑定连接器: xxx"``——
    指纹只吃**错误码**（失败模式聚类），不吃自由文本消息（同一失败的不同措辞归同一簇）。
    """
    code = (raw or "").split(":", 1)[0].strip()
    return code.casefold()


def scenario_fingerprint(
    kind: GapKind,
    *,
    intent_key: str = "",
    action_iri: str = "",
    entity_types: tuple[str, ...] | list[str] = (),
    failure_mode: str = "",
) -> str:
    """场景指纹（§13.3 G0「意图向量 + 实体类型集 + 失败模式」的规范化拼接，口径 v1）。

    canonical = ``"\\x1f".join([GAP_FINGERPRINT_VERSION, kind.value, 主键, 实体类型集, 失败模式])``
    → sha256 hex。主键：unmapped_intent/manual_fallback 走 ``intent_key`` 规范化（意图向量），
    unbound_action/execution_failure 走 ``action_iri`` 原样去空白（IRI 大小写敏感）。
    同一场景的不同书写格式（大小写/空白/错误码消息措辞/实体类型顺序）归一同指纹。
    """
    primary = _norm_key(intent_key) if intent_key else action_iri.strip()
    canonical = "\x1f".join(
        [
            GAP_FINGERPRINT_VERSION,
            kind.value,
            primary,
            "|".join(sorted(set(entity_types))),
            norm_failure_mode(failure_mode),
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class GapEvent:
    """一次缺口事件（G0 采集的最小单元；四型共用，主键按型二选一必填）。

    必填：``tenant_id`` / ``kind`` / ``occurred_at`` / ``source``；主键：
    ``action_iri``（unbound_action / execution_failure）或 ``intent_key``
    （unmapped_intent；manual_fallback 两者皆可）。``fingerprint`` 构造期自动计算
    （口径见 ``scenario_fingerprint``）；``entity_types``/``failure_mode`` 为指纹输入，
    ``detail`` 为不入指纹的旁证（ledger_id/attempts 等）。
    """

    tenant_id: uuid.UUID
    kind: GapKind
    occurred_at: datetime
    source: str  # 信号源标识（如 writeback.ledger.failed / writeback.dispatcher.resolve_miss / demo）
    action_iri: str | None = None
    intent_key: str | None = None
    entity_types: tuple[str, ...] = ()  # 实体类型集（指纹输入 v1；顺序不敏感）
    failure_mode: str = ""  # 失败模式原始串（指纹只取规范化错误码段）
    trace_ids: tuple[str, ...] = ()  # 来源轨迹（证据链起点；进工单 source_trace_ids）
    detail: dict[str, Any] = field(default_factory=dict)  # 旁证（不入指纹）
    fingerprint: str = ""  # 场景指纹（构造期自动计算；显式传入仅限重放校验）

    def __post_init__(self) -> None:
        if self.kind in (GapKind.UNBOUND_ACTION, GapKind.EXECUTION_FAILURE) and not self.action_iri:
            raise ValueError(f"{self.kind.value} 事件必须携带 action_iri（行动类 IRI，§13.3 G0）")
        if self.kind is GapKind.UNMAPPED_INTENT and not self.intent_key:
            raise ValueError("unmapped_intent 事件必须携带 intent_key（任务无法归类的意图向量）")
        if self.kind is GapKind.MANUAL_FALLBACK and not (self.action_iri or self.intent_key):
            raise ValueError("manual_fallback 事件必须携带 action_iri 或 intent_key 之一")
        if self.fingerprint and self.fingerprint != self.computed_fingerprint():
            raise ValueError("fingerprint 与规范口径不符（重放校验失败；一般应留空自动计算）")
        if not self.fingerprint:
            self.fingerprint = self.computed_fingerprint()

    def computed_fingerprint(self) -> str:
        """按 v1 口径计算场景指纹（同口径同指纹，可独立复算校验）。"""
        return scenario_fingerprint(
            self.kind,
            intent_key=self.intent_key or "",
            action_iri=self.action_iri or "",
            entity_types=self.entity_types,
            failure_mode=self.failure_mode,
        )

    def primary_key(self) -> str:
        """主键展示值（报告/样本用：intent_key 或 action_iri）。"""
        return self.intent_key or self.action_iri or ""

    # ------------------------------------------------------------- JSONL 序列化

    def to_json(self) -> str:
        """单行 JSON（JsonlGapStore 落盘形；字段自描述可审计可复现）。"""
        payload: dict[str, Any] = {
            "tenant_id": str(self.tenant_id),
            "kind": self.kind.value,
            "occurred_at": self.occurred_at.isoformat(),
            "source": self.source,
            "fingerprint": self.fingerprint,
            "fingerprint_version": GAP_FINGERPRINT_VERSION,
            "entity_types": list(self.entity_types),
            "failure_mode": self.failure_mode,
            "trace_ids": list(self.trace_ids),
            "detail": dict(self.detail),
        }
        if self.action_iri is not None:
            payload["action_iri"] = self.action_iri
        if self.intent_key is not None:
            payload["intent_key"] = self.intent_key
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> GapEvent:
        """单行 JSON → GapEvent（fingerprint 走重放校验：口径漂移即报错，不静默混算）。"""
        payload = json.loads(line)
        return cls(
            tenant_id=uuid.UUID(payload["tenant_id"]),
            kind=GapKind(payload["kind"]),
            occurred_at=datetime.fromisoformat(payload["occurred_at"]),
            source=payload["source"],
            action_iri=payload.get("action_iri"),
            intent_key=payload.get("intent_key"),
            entity_types=tuple(payload.get("entity_types") or ()),
            failure_mode=payload.get("failure_mode") or "",
            trace_ids=tuple(payload.get("trace_ids") or ()),
            detail=dict(payload.get("detail") or {}),
            fingerprint=payload.get("fingerprint") or "",
        )


@dataclass(slots=True)
class GapProposalRecord:
    """缺口工单开单留痕（指纹 → proposal 关联；去重与审计的落盘形）。"""

    fingerprint: str
    kind: str
    proposal_id: str
    tenant_id: str
    cluster_size: int
    window_days: int
    opened_at: str  # ISO 时刻
    surface: str = EvolutionSurface.TOOL_IMPL.value  # G0 缺口工单固定开在 O1 面（§13.3）

    def to_json(self) -> str:
        return json.dumps(
            {
                "fingerprint": self.fingerprint,
                "kind": self.kind,
                "proposal_id": self.proposal_id,
                "tenant_id": self.tenant_id,
                "cluster_size": self.cluster_size,
                "window_days": self.window_days,
                "opened_at": self.opened_at,
                "surface": self.surface,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, line: str) -> GapProposalRecord:
        payload = json.loads(line)
        return cls(
            fingerprint=payload["fingerprint"],
            kind=payload["kind"],
            proposal_id=payload["proposal_id"],
            tenant_id=payload["tenant_id"],
            cluster_size=int(payload["cluster_size"]),
            window_days=int(payload["window_days"]),
            opened_at=payload["opened_at"],
            surface=payload.get("surface") or EvolutionSurface.TOOL_IMPL.value,
        )


@runtime_checkable
class GapStore(Protocol):
    """缺口事件/工单留痕存储端口（组合根可换 PG 态；同步面——dispatcher emit 为同步回调）。"""

    def append_event(self, event: GapEvent) -> None: ...  # pragma: no cover — Protocol 无实现

    def load_events(self) -> list[GapEvent]: ...  # pragma: no cover — Protocol 无实现

    def append_proposal_record(self, record: GapProposalRecord) -> None: ...  # pragma: no cover

    def load_proposal_records(self) -> list[GapProposalRecord]: ...  # pragma: no cover


class InMemoryGapStore:
    """内存存储（测试/进程内采集；不落盘）。"""

    def __init__(self) -> None:
        self.events: list[GapEvent] = []
        self.proposal_records: list[GapProposalRecord] = []

    def append_event(self, event: GapEvent) -> None:
        self.events.append(event)

    def load_events(self) -> list[GapEvent]:
        return list(self.events)

    def append_proposal_record(self, record: GapProposalRecord) -> None:
        self.proposal_records.append(record)

    def load_proposal_records(self) -> list[GapProposalRecord]:
        return list(self.proposal_records)


class JsonlGapStore:
    """JSONL 追加写落盘（events 与 proposals 两文件，目录可注入）。

    **PG rsi_proposals 表随改表流程批次（06 契约 → database/01 DDL → Alembic 迁移），
    v1 文件态可审计可复现**：每行自描述 JSON（含指纹口径版本），重放 ``from_json`` 即复算
    校验。追加写-only，不就地改写（审计纪律：只前进不抹除）。
    """

    EVENTS_FILENAME = "gap_events.jsonl"
    PROPOSALS_FILENAME = "gap_proposals.jsonl"

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def events_path(self) -> Path:
        return self._dir / self.EVENTS_FILENAME

    @property
    def proposals_path(self) -> Path:
        return self._dir / self.PROPOSALS_FILENAME

    def append_event(self, event: GapEvent) -> None:
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(event.to_json() + "\n")

    def load_events(self) -> list[GapEvent]:
        return self._load_lines(self.events_path, GapEvent.from_json)

    def append_proposal_record(self, record: GapProposalRecord) -> None:
        with self.proposals_path.open("a", encoding="utf-8") as fh:
            fh.write(record.to_json() + "\n")

    def load_proposal_records(self) -> list[GapProposalRecord]:
        return self._load_lines(self.proposals_path, GapProposalRecord.from_json)

    @staticmethod
    def _load_lines(path: Path, parse: Callable[[str], _T]) -> list[_T]:
        if not path.exists():
            return []
        items: list[_T] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    items.append(parse(stripped))
        return items


@dataclass(slots=True)
class GapClusterSummary:
    """聚类摘要（运行报告行粒度：簇指纹/规模/处置结论）。"""

    tenant_id: str
    kind: str
    fingerprint: str
    size: int
    disposition: str  # opened | below_threshold | duplicate_ticket（已有未闭合工单，去重）
    proposal_id: str | None = None  # disposition=opened 时非空


@dataclass(slots=True)
class GapEvaluation:
    """一轮 ``GapCollector.evaluate`` 的结果（开单 + 全簇摘要，供运行报告）。"""

    opened: list[Proposal]
    clusters: list[GapClusterSummary]
    window_days: int
    threshold: int
    evaluated_at: datetime
    events_in_window: int


class GapCollector:
    """G0 缺口采集器：``collect`` 落事件 → ``evaluate`` 滑窗聚类达标开单（全确定性零 LLM）。

    - ``collect``：滑窗累计的写入侧——事件原样落 ``GapStore``（滑窗过滤在读侧做，落盘
      保留全量事件，重放可复现任意窗口的聚类）；
    - ``evaluate``：窗口（默认 30 天）内按（租户, 指纹）分组，簇规模 ≥ 阈值（默认 5 次）
      即开缺口工单：``RsiService.submit`` 入池（status=draft、surface=O1、evidence={簇规模/
      窗口/指纹/样本}， ImprovementType=tool_description——§13.6 白名单第 ⑤ 类收编进 O1 面）；
      同一（租户, 指纹）已有**未闭合工单**则不重复开（去重，口径见 ``_has_unresolved_ticket``）。
    """

    def __init__(
        self,
        *,
        store: GapStore,
        rsi: RsiService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._rsi = rsi
        self._now = clock or (lambda: datetime.now(UTC))

    async def collect(self, event: GapEvent) -> None:
        """采集一条缺口事件（追加落 store；不触发聚类——开单由 evaluate 显式驱动）。"""
        self._store.append_event(event)

    async def evaluate(
        self,
        *,
        window_days: int = 30,
        threshold: int = 5,
        now: datetime | None = None,
        sample_size: int = DEFAULT_SAMPLE_SIZE,
    ) -> GapEvaluation:
        """滑窗聚类 → 达标簇开缺口工单（同指纹 open draft 去重；返回开单 + 全簇摘要）。"""
        moment = now or self._now()
        window_start = moment - timedelta(days=window_days)
        events = [e for e in self._store.load_events() if window_start <= e.occurred_at <= moment]
        groups: dict[tuple[uuid.UUID, str], list[GapEvent]] = {}
        for event in events:
            groups.setdefault((event.tenant_id, event.fingerprint), []).append(event)

        opened: list[Proposal] = []
        clusters: list[GapClusterSummary] = []
        # 规模降序（达标簇优先处置）；同规模按指纹稳定排序（确定性输出，重放同序）
        ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0][1]))
        for (tenant_id, fingerprint), cluster_events in ranked:
            size = len(cluster_events)
            if size < threshold:
                clusters.append(
                    GapClusterSummary(
                        tenant_id=str(tenant_id),
                        kind=cluster_events[0].kind.value,
                        fingerprint=fingerprint,
                        size=size,
                        disposition="below_threshold",
                    )
                )
                continue
            if self._has_unresolved_ticket(tenant_id, fingerprint):
                clusters.append(
                    GapClusterSummary(
                        tenant_id=str(tenant_id),
                        kind=cluster_events[0].kind.value,
                        fingerprint=fingerprint,
                        size=size,
                        disposition="duplicate_ticket",
                    )
                )
                continue
            proposal = await self._open_ticket(
                tenant_id=tenant_id,
                fingerprint=fingerprint,
                cluster_events=cluster_events,
                window_days=window_days,
                threshold=threshold,
                sample_size=sample_size,
            )
            opened.append(proposal)
            clusters.append(
                GapClusterSummary(
                    tenant_id=str(tenant_id),
                    kind=cluster_events[0].kind.value,
                    fingerprint=fingerprint,
                    size=size,
                    disposition="opened",
                    proposal_id=str(proposal.id),
                )
            )
        return GapEvaluation(
            opened=opened,
            clusters=clusters,
            window_days=window_days,
            threshold=threshold,
            evaluated_at=moment,
            events_in_window=len(events),
        )

    # ------------------------------------------------------------- 内部

    def _has_unresolved_ticket(self, tenant_id: uuid.UUID, fingerprint: str) -> bool:
        """同（租户, 指纹）是否存在未闭合工单（去重判据，保守方向）。

        - 池内可见且 status=draft → 未闭合，不重复开；
        - 留痕存在但**池内不可见**（跨进程/服务重启，v1 文件态无法核实其闭合）→ 按未闭合
          处理，不重复开（宁可漏开一簇的重复工单，不可重复轰炸审核面）；
        - 复开仅当池内**可核实**前单已达非 draft（evaluated/rejected 等，经 proposal.transition）
          ——跨进程的工单状态回写随 PG rsi_proposals 批次交付。
        """
        for record in self._store.load_proposal_records():
            if record.fingerprint != fingerprint or record.tenant_id != str(tenant_id):
                continue
            try:
                proposal_id = uuid.UUID(record.proposal_id)
            except ValueError:
                continue
            proposal = self._rsi.pool.get(proposal_id)
            if proposal is None or proposal.status is ProposalStatus.DRAFT:
                return True
        return False

    async def _open_ticket(
        self,
        *,
        tenant_id: uuid.UUID,
        fingerprint: str,
        cluster_events: list[GapEvent],
        window_days: int,
        threshold: int,
        sample_size: int,
    ) -> Proposal:
        """达标簇 → 缺口工单（draft；surface=O1；evidence={簇规模/窗口/指纹/样本}）。"""
        kind = cluster_events[0].kind
        size = len(cluster_events)
        latest = sorted(cluster_events, key=lambda e: e.occurred_at, reverse=True)
        evidence = {
            "cluster_size": size,
            "window_days": window_days,
            "threshold": threshold,
            "fingerprint": fingerprint,
            "fingerprint_version": GAP_FINGERPRINT_VERSION,
            "first_seen": min(e.occurred_at for e in cluster_events).isoformat(),
            "last_seen": max(e.occurred_at for e in cluster_events).isoformat(),
            "samples": [
                {
                    "occurred_at": e.occurred_at.isoformat(),
                    "source": e.source,
                    "primary_key": e.primary_key(),
                    "failure_mode": norm_failure_mode(e.failure_mode),
                    "trace_ids": list(e.trace_ids),
                }
                for e in latest[:sample_size]
            ],
        }
        envelope: dict[str, Any] = {
            # G0 只开单不起草（零 LLM 红线）：patch 留空为 G1 起草位，非信封缺键（键存在）
            "patch": None,
            "expected_gain": f"补齐缺口场景的工具实现（簇 {size} 次/{window_days} 天，指纹 {fingerprint[:12]}）",
            "risk_level": "low",  # 候选未生效，生效走 G2 门禁 + G3 三档转正
            "eval_plan": {"replay": "G0 簇场景回放（G2 沉淀黄金场景集）", "threshold": None},
            "gap": {
                "track": "gap",  # 缺口轨（09 §13.4 第三轨）
                "stage": "G0",  # 工具进化链路阶段（§13.3 G0→G1→G2→G3）
                "surface": EvolutionSurface.TOOL_IMPL.value,  # 缺口工单固定开在 O1 面
                "surface_name": "工具实现",
                "kind": kind.value,
                "evidence": evidence,
                # G1 三级降路径提示（§13.3 G1 逐字；本批只提示不执行）
                "g1_draft_path_hint": (
                    "①组合既有工具（最低风险，落 O5 面计划模板）→ "
                    "②市场能力包检索安装（L2 通道，走既有审核）→ "
                    "③LLM 起草候选工具（须带行动类语义标注 + executionMode 声明，无标注不上架）"
                ),
            },
        }
        proposal = await self._rsi.submit(
            tenant_id=tenant_id,
            type_str=ImprovementType.TOOL_DESCRIPTION.value,
            target=f"{GAP_TARGET_PREFIX}/gap-{kind.value}-{fingerprint[:12]}@v0",
            envelope=envelope,
            trigger=TriggerTrack.GAP,
            source_trace_ids=tuple(dict.fromkeys(tid for e in cluster_events for tid in e.trace_ids)),
        )
        self._store.append_proposal_record(
            GapProposalRecord(
                fingerprint=fingerprint,
                kind=kind.value,
                proposal_id=str(proposal.id),
                tenant_id=str(tenant_id),
                cluster_size=size,
                window_days=window_days,
                opened_at=self._now().isoformat(),
            )
        )
        return proposal
