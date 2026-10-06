"""ORSI G1 起草引擎（architecture/09 §13.3 G1：确定性优先 → agent 的三级降路径）。

G1 在 G0 开出的缺口工单（trigger=gap、status=draft、evidence 含簇样本）上起草改进草案，
按 §13.3 逐字的三级降路径**顺序串联、首次命中即返**（降路径语义：低风险级优先，全 miss
如实降级）：

- **L1 组合既有工具**（确定性，零 LLM）：按工单 action_iri 在**种子本体行动类**
  （services/seeds/power_seed.ttl 的 ob2:Action 子类闭包）与**平台既有工具绑定**
  （writeback ConnectorRegistry 的 action_iris，注入形）中找**同域（同命名空间）行动类**，
  编排为计划模板草案；
- **L2 市场能力包检索**（确定性检索）：经 plugin 市场列表/检索面
  （PluginMarketService.list_market + get_detail 的结构子集 Protocol）按行动类语义标注/
  关键词检索已发布能力包。**plugin 侧只读不装**——L2 只产候选包引用，安装走既有市场
  审核与两级验签链（Skills §4/§5.1「走既有审核」），本模块零安装副作用；
- **L3 LLM 起草候选工具**（本链唯一 agent 环节）：ModelPort.complete_structured 受约束生成，
  产物过**确定性校验**（宪法推理分级：LLM 只做低频语义判断且输出必须过校验）——
  action_iri 必须落在种子行动类集内、execution_mode 枚举合法、description 非空，
  **任何越界=拒绝该次起草**（「无语义标注不上架」铁律，零幻觉上架），重试预算硬上限
  ``L3_MAX_RETRIES``，耗尽=该工单本轮起草失败（工单保持 draft，报告记录）。

进化面落位（§13.2 分界铁律，承载于 ``DraftArtifact.surface``）：**L1 组合落 O5 控制流程**
——组合的是既有行动类的编排（计划模板固化），不新增任何实现绑定；**L2/L3 落 O1 工具实现**
——市场包/LLM 草案是行动类的新实现绑定，不得触碰行动类语义（语义要变即 O2 走 changeset
人工终审）。SHACL 基线与公理永远不在任何进化面内（红线 §6.2）。

生命周期红线：本模块**只写 ``proposal.envelope["draft_artifact"]``（JSONB 整体重赋值纪律：
新字典整体替换，不做嵌套就地改写），不迁移候选状态**——draft→evaluated→… 的唯一迁移入口
在 RsiService/Proposal.transition，G2 测试与 G3 三档转正本批不做也不 stub（09 §13.3）。
产物是**候选非成品**：任何路径的草案都不直接生效（apply 恒拒红线独立生效）。

本文件因依赖 ontology.core.tbox（种子装载）与 platform.ports（模型端口）**不进包根命名空间**
（sinks.py 同款纪律，防包根 import 拉起跨模块边）——直 ``from services.rsi.drafter import …``。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from rdflib import Graph
from rdflib.namespace import RDFS

from services.ontology.core.tbox import OB2, load_turtle
from services.platform.ports.model_port import ModelPort, ModelPortError
from services.rsi.proposal import Proposal, TriggerTrack
from services.rsi.surfaces import EvolutionSurface

# 种子本体行动类装载缺省位（services/seeds/power_seed.ttl，09 §13.3 G1 定稿路径随种子落位调整）
DEFAULT_SEED_PATH = Path(__file__).resolve().parents[1] / "seeds" / "power_seed.ttl"

# L3 重试预算硬上限（初始 1 次 + 重试 ≤2 = 至多 3 次调用；耗尽=该工单本轮起草失败）
L3_MAX_RETRIES = 2

# L2 市场单轮扫描上限（已发布过滤**后**的确定性截断——draft/in_review 新条目不得挤占候选窗）
L2_MARKET_SCAN_LIMIT = 50

# 已发布状态串（duck-typed 过滤口径：PluginStatus.PUBLISHED 的 StrEnum 值；服务端经
# to_storage_status 投影，客户端按字符串等值复滤——两端口径同串）
_PUBLISHED_STATUS = "published"

# L1 计划模板编排步数上限（组合方案保持最小够用，防同域行动类全集堆砌）
L1_MAX_STEPS = 5


class DraftPath(StrEnum):
    """三级降路径命中值（09 §13.3 G1：①组合 ②市场 ③LLM）。"""

    COMBINATION = "combination"  # ① 组合既有工具（最低风险，落 O5 计划模板）
    MARKET = "market"  # ② 市场能力包检索（L2 通道，走既有审核）
    LLM = "llm"  # ③ LLM 起草候选工具（agent 环节，产物过确定性校验）


class ToolExecutionMode(StrEnum):
    """起草产物的工具实现形态声明（09 §13.3 G1：MCP server / CLI 包装 / API 调用链脚本，
    组合路径固化为计划模板）。

    与行动类自身的 ``ob2:executionMode``（readOnly|stateful|externalWrite|code，ontology
    §2.2 门禁强度标注）是两套词表：前者声明**工具如何实现**（G1 起草产物必带，L3 必填），
    后者声明**行动类的门禁强度**（随行动类定义，O1 面不得触碰）。
    """

    MCP_SERVER = "mcp_server"  # MCP server 形态
    CLI_WRAPPER = "cli_wrapper"  # CLI 包装形态
    API_SCRIPT = "api_script"  # API 调用链脚本形态
    PLAN_TEMPLATE = "plan_template"  # 计划模板形态（L1 组合路径固定值）


# 三级置信度定档（确定性组合 > 市场既有包 > LLM 草案；G2 门禁独立裁决，不采信本值）
CONFIDENCE_L1 = 0.9
CONFIDENCE_L2 = 0.7
CONFIDENCE_L3 = 0.5

# 市场插件 kind → 工具实现形态映射（无法映射的 kind 不强赋，execution_mode 如实留 None）
_KIND_TO_EXECUTION_MODE: dict[str, ToolExecutionMode] = {
    "mcp_server": ToolExecutionMode.MCP_SERVER,
    "rest_api": ToolExecutionMode.API_SCRIPT,
}


@dataclass(frozen=True, slots=True)
class DraftArtifact:
    """G1 起草产物（三级降路径首次命中的草案；候选非成品——生效走 G2 门禁 + G3 三档转正）。

    surface 分界铁律（§13.2）：L1 组合=O5 控制流程（既有行动类编排成计划模板，不新增实现
    绑定）；L2 市场/L3 LLM=O1 工具实现（行动类的新实现绑定，不得触碰行动类语义——语义要变
    即 O2 走 changeset 人工终审）。

    ``execution_mode``：L3 必填（「无语义标注不上架」）；L1 固定 plan_template；L2 按市场
    包 kind 映射（mcp_server/rest_api），映射不上的如实留 None。
    """

    path: DraftPath
    surface: EvolutionSurface
    content: dict[str, Any]
    action_iri: str
    execution_mode: ToolExecutionMode | None
    confidence: float
    rationale: str
    drafted_at: str  # ISO 时刻（运行报告/审计回链）

    def to_payload(self) -> dict[str, Any]:
        """信封写回落形（envelope["draft_artifact"] 的值；JSONB 整体重赋值的载荷）。"""
        return {
            "path": self.path.value,
            "surface": self.surface.value,
            "content": dict(self.content),
            "action_iri": self.action_iri,
            "execution_mode": self.execution_mode.value if self.execution_mode else None,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "drafted_at": self.drafted_at,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> DraftArtifact:
        """落形 → DraftArtifact（测试/重放校验用；缺键抛 KeyError 如实暴露口径漂移）。"""
        mode = payload.get("execution_mode")
        return cls(
            path=DraftPath(payload["path"]),
            surface=EvolutionSurface(payload["surface"]),
            content=dict(payload["content"]),
            action_iri=payload["action_iri"],
            execution_mode=ToolExecutionMode(mode) if mode else None,
            confidence=float(payload["confidence"]),
            rationale=payload["rationale"],
            drafted_at=payload["drafted_at"],
        )


@dataclass(frozen=True, slots=True)
class DraftAttempt:
    """单级起草尝试结论（运行报告行粒度：命中/落空/跳过/拒绝/端口错误）。"""

    level: str  # L1 | L2 | L3
    outcome: str  # hit | miss | skipped | rejected | port_error
    detail: str

    def to_payload(self) -> dict[str, str]:
        return {"level": self.level, "outcome": self.outcome, "detail": self.detail}


# ---------------------------------------------------------------- 种子行动类装载（只读复用）


def seed_actions_from_graph(graph: Graph) -> frozenset[str]:
    """rdflib 图 → 行动类 IRI 集（rdfs:subClassOf 传递闭包触及 ob2:Action 的全部类）。

    只读复用：种子是专家定稿资产（lint 门禁自证随 ontology 侧），本函数不校验不修改。
    """
    children: dict[Any, set[Any]] = {}
    for child, parent in graph.subject_objects(RDFS.subClassOf):  # (subject=子类, object=父类)
        children.setdefault(parent, set()).add(child)
    actions: set[str] = set()
    visited: set[Any] = set()
    frontier: list[Any] = [OB2.Action]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, ()):
            if child in visited:
                continue
            visited.add(child)
            actions.add(str(child))
            frontier.append(child)
    return frozenset(actions)


def load_seed_actions(path: Path | str = DEFAULT_SEED_PATH) -> frozenset[str]:
    """种子 Turtle（缺省 services/seeds/power_seed.ttl）→ 行动类 IRI 集（复用 ontology.core 既有装载器）。"""
    graph = load_turtle(Path(path).read_text(encoding="utf-8"))
    return seed_actions_from_graph(graph)


# ---------------------------------------------------------------- 缺口工单上下文提取


def _namespace(iri: str) -> str:
    """IRI 命名空间（fragment 或路径末段前缀）——「同域」判定口径。"""
    if "#" in iri:
        return iri.rsplit("#", 1)[0]
    return iri.rsplit("/", 1)[0]


_CAMEL_SPLIT = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def _local_tokens(iri_or_key: str) -> tuple[str, ...]:
    """行动类本地名/意图键 → 检索词元（camelCase 拆分 + casefold；L2 关键词匹配口径）。"""
    local = iri_or_key.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    return tuple(token.casefold() for token in _CAMEL_SPLIT.findall(local) if token)


# 需要行动类 IRI 的缺口型（unmapped_intent 语义上无行动类——L1 无域锚点，L3 须自种子集内提出）
_ACTION_BEARING_KINDS = frozenset({"unbound_action", "execution_failure"})


def gap_action_context(proposal: Proposal) -> tuple[str, tuple[str, ...]]:
    """缺口工单 → (行动类 IRI, 检索词元)。

    - unbound_action / execution_failure：簇样本主键即行动类 IRI（gap.py 开单口径）；
    - manual_fallback：主键为 IRI 形（含 ``://`` 或 ``#``）则取，否则无行动类；
    - unmapped_intent：任务无法归类到任何行动类（该型本义）→ 空串，词元取意图键。
    """
    gap = proposal.envelope.get("gap") or {}
    kind = str(gap.get("kind") or "")
    samples = (gap.get("evidence") or {}).get("samples") or []
    primary = str((samples[0] or {}).get("primary_key") or "") if samples else ""
    if kind in _ACTION_BEARING_KINDS:
        return primary, _local_tokens(primary)
    if "://" in primary or "#" in primary:
        return primary, _local_tokens(primary)
    return "", _local_tokens(primary)


# ---------------------------------------------------------------- L1 组合（确定性，零 LLM）


class L1ComposeStep:
    """① 组合既有工具（确定性）：同域（同命名空间）既有行动类 → 计划模板草案（surface=O5）。

    搜索空间 = 种子行动类 ∪ 平台既有工具绑定（writeback ConnectorRegistry 的 action_iris
    注入形；无注入=空集）。工单行动类自身**不入编排**——缺口恰在于它（未绑定/在失败），
    组合的是其余既有行动类。命中需至少一个同域候选；无 → None 降 L2。
    """

    def run(
        self,
        proposal: Proposal,
        *,
        action_iri: str,
        seed_actions: Iterable[str],
        registry_iris: Iterable[str],
        now: datetime,
    ) -> DraftArtifact | None:
        _ = proposal  # 工单上下文已由调用方折出 action_iri（签名保留工单位：报告/审计回链）
        if not action_iri:
            return None  # 无行动类锚点（unmapped_intent/无 IRI 主键）→ 无域可组合，降 L2
        seeds = frozenset(seed_actions)
        bound = frozenset(registry_iris)
        domain = _namespace(action_iri)
        candidates = sorted(iri for iri in seeds | bound if iri != action_iri and _namespace(iri) == domain)
        if not candidates:
            return None
        # 既有工具优先（registry 绑定=今天就可执行），组内 IRI 稳定排序（确定性输出，重放同序）
        ordered = sorted(candidates, key=lambda iri: (iri not in bound, iri))[:L1_MAX_STEPS]
        steps = [{"seq": idx, "action_iri": iri, "bound": iri in bound} for idx, iri in enumerate(ordered, start=1)]
        bound_count = sum(1 for iri in ordered if iri in bound)
        return DraftArtifact(
            path=DraftPath.COMBINATION,
            surface=EvolutionSurface.CONTROL_FLOW,  # 组合落 O5（§13.2 分界铁律）
            content={
                "plan_template": {
                    "composed_for": action_iri,
                    "steps": steps,
                    "step_count": len(steps),
                    "bound_count": bound_count,
                }
            },
            action_iri=action_iri,
            execution_mode=ToolExecutionMode.PLAN_TEMPLATE,
            confidence=CONFIDENCE_L1,
            rationale=(
                f"同域（命名空间 {domain}）既有行动类 {len(candidates)} 个可编排为计划模板"
                f"（registry 已绑定 {bound_count} 个）——零新增实现、零 LLM，落 O5 控制流程面"
                f"（09 §13.3 ① / §13.2 分界铁律）"
            ),
            drafted_at=now.isoformat(),
        )


# ---------------------------------------------------------------- L2 市场检索（确定性，plugin 只读）


@runtime_checkable
class MarketSearchPort(Protocol):
    """市场检索面（PluginMarketService 的结构子集；plugin 侧只读不装——走既有审核）。

    ``list_market``/``get_detail`` 按 plugin.business.lifecycle 实际签名适配（keyword-only
    status/offset/limit；版本树倒序由仓储保证）。L2 只读检索产候选包引用，
    **不调用任何安装/启用面**。
    """

    async def list_market(
        self, *, status: Any = None, offset: int = 0, limit: int = 20
    ) -> Sequence[Any]: ...  # pragma: no cover — Protocol 方法无实现

    async def get_detail(self, plugin_id: uuid.UUID) -> tuple[Any, Sequence[Any]]: ...  # pragma: no cover


def _manifest_tool_annotations(server_json: Mapping[str, Any]) -> list[dict[str, Any]]:
    """server_json → 工具声明精简投影（name/description/action_iri；镜像 manifest_tools 口径）。

    不 import plugin 域模型（跨模块边最小化）：x-platform.tools[] 优先，顶层 tools[] 兼容，
    行动类语义标注取 semantic_annotation.action_iri（Skills §3.4.2「无标注不上架」同源位）。
    """
    x_platform = server_json.get("x-platform")
    raw_tools = (x_platform.get("tools") if isinstance(x_platform, Mapping) else None) or server_json.get("tools") or []
    annotations: list[dict[str, Any]] = []
    for item in raw_tools if isinstance(raw_tools, Sequence) else ():
        if not isinstance(item, Mapping):
            continue
        semantic = item.get("semantic_annotation")
        annotations.append(
            {
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "action_iri": str(semantic.get("action_iri") or "") if isinstance(semantic, Mapping) else "",
            }
        )
    return annotations


def _latest_published_version(versions: Sequence[Any]) -> Any | None:
    """版本树（倒序）→ 最新已发布版本（无已发布版本取首版作匹配面；只读，不触版本状态）。"""
    fallback: Any | None = None
    for version in versions:
        status = str(getattr(version, "status", ""))
        if status == "published":
            return version
        if fallback is None:
            fallback = version
    return fallback


class L2MarketStep:
    """② 市场能力包检索（确定性检索）：按行动类语义标注/关键词找已发布包 → 候选包引用（surface=O1）。

    匹配优先级：工具语义标注 action_iri 精确命中 > 关键词全命中（词元 ⊆ 包可检索文本）。
    **plugin 侧只读不装**：产物只携带候选包引用（安装走既有市场审核 + 两级验签 +
    PluginMarketService.install，本步零安装副作用）；无命中 → None 降 L3。

    扫描口径（ocr 评审 ③）：**先按已发布过滤再截断**——服务端过滤优先（list_market 传
    ``status="published"``，PluginMarketService 经 to_storage_status 投影生效），客户端对
    返回窗再复滤已发布并截断至 ``L2_MARKET_SCAN_LIMIT`` 件（确定性截断，draft/in_review
    新条目不得挤占已发布候选窗）。逐件 ``get_detail`` 容错：LookupError（并发下架/删除）
    跳过该件继续扫描，跳过数随结论文本留痕（ocr 评审 ⑤）。
    """

    async def run_market(
        self,
        proposal: Proposal,
        *,
        action_iri: str,
        keywords: Iterable[str],
        market: MarketSearchPort | None,
        now: datetime,
    ) -> tuple[DraftArtifact | None, str]:
        """检索一轮市场（返回 (草案 | None, 结论文本)；结论文本进运行报告的尝试留痕）。"""
        if market is None:
            return None, "market 未注入（L2 通道不可用）"
        plugins = await self._list_published(market)
        tokens = tuple(keywords)
        keyword_match: tuple[Any, Any, dict[str, Any]] | None = None
        vanished = 0
        for plugin in plugins:
            plugin_id = getattr(plugin, "id", None)
            if plugin_id is None:
                continue
            try:
                detail = await market.get_detail(plugin_id)
            except LookupError:  # 并发下架/删除：跳过该件继续扫描（单件故障不炸整链）
                vanished += 1
                continue
            versions = detail[1] if isinstance(detail, tuple) else ()
            version = _latest_published_version(versions)
            if version is None:
                continue
            for tool in _manifest_tool_annotations(getattr(version, "server_json", {}) or {}):
                if action_iri and tool["action_iri"] == action_iri:
                    artifact = self._artifact(proposal, plugin, version, tool, "action_iri", now)
                    return artifact, (
                        f"市场包 `{getattr(plugin, 'slug', plugin_id)}` 工具 `{tool['name']}` "
                        f"语义标注精确命中 {action_iri}" + (f"（跳过并发消失件 {vanished} 件）" if vanished else "")
                    )
                if keyword_match is None and tokens and self._keyword_hit(tool, plugin, tokens):
                    keyword_match = (plugin, version, tool)
        if keyword_match is not None:
            plugin, version, tool = keyword_match
            artifact = self._artifact(proposal, plugin, version, tool, "keyword", now)
            return artifact, (
                f"市场包 `{getattr(plugin, 'slug', '')}` 工具 `{tool['name']}` 关键词命中"
                f"（词元 {'+'.join(tokens)}）" + (f"（跳过并发消失件 {vanished} 件）" if vanished else "")
            )
        return (
            None,
            f"市场扫描已发布 {len(plugins)} 件（截断 {L2_MARKET_SCAN_LIMIT}）无行动类语义/关键词命中"
            + (f"（跳过并发消失件 {vanished} 件）" if vanished else ""),
        )

    async def _list_published(self, market: MarketSearchPort) -> Sequence[Any]:
        """已发布件列表：服务端过滤优先，客户端复滤后截断（口径见类 docstring）。"""
        try:
            plugins = await market.list_market(status=_PUBLISHED_STATUS, limit=L2_MARKET_SCAN_LIMIT)
        except TypeError:  # 端口不收 status 参数（ duck-typed 面兼容位）：退化为客户端复滤
            plugins = await market.list_market(limit=L2_MARKET_SCAN_LIMIT)
        return [plugin for plugin in plugins if str(getattr(plugin, "status", "")) == _PUBLISHED_STATUS][
            :L2_MARKET_SCAN_LIMIT
        ]

    @staticmethod
    def _keyword_hit(tool: Mapping[str, Any], plugin: Any, tokens: Sequence[str]) -> bool:
        """关键词全命中判定（确定性）：全部词元 ⊆ 包可检索文本（slug/名称/形态/工具名与描述）。"""
        haystack = " ".join(
            str(part).casefold()
            for part in (
                getattr(plugin, "slug", ""),
                getattr(plugin, "name", ""),
                getattr(plugin, "kind", ""),
                tool.get("name", ""),
                tool.get("description", ""),
            )
        )
        return all(token in haystack for token in tokens)

    def _artifact(
        self,
        proposal: Proposal,
        plugin: Any,
        version: Any,
        tool: Mapping[str, Any],
        match: str,
        now: datetime,
    ) -> DraftArtifact:
        kind = str(getattr(plugin, "kind", "") or "")
        return DraftArtifact(
            path=DraftPath.MARKET,
            surface=EvolutionSurface.TOOL_IMPL,  # 市场包=行动类新实现绑定（§13.2 分界铁律）
            content={
                "market_ref": {
                    "plugin_id": str(getattr(plugin, "id", "")),
                    "slug": str(getattr(plugin, "slug", "")),
                    "name": str(getattr(plugin, "name", "")),
                    "kind": kind,
                    "version": str(getattr(version, "version", "") or ""),
                    "matched_tool": str(tool.get("name", "")),
                    "matched_action_iri": str(tool.get("action_iri", "")),
                    "match": match,
                },
                # 走既有审核（plugin 侧只读不装）：安装/启用/两级验签均在市场既有链路
                "install_note": (
                    "L2 只产候选包引用：安装走既有市场审核与两级验签链"
                    "（PluginMarketService.install/enable），本步零安装副作用"
                ),
            },
            action_iri=str(tool.get("action_iri", "")) or gap_action_context(proposal)[0],
            execution_mode=_KIND_TO_EXECUTION_MODE.get(kind),
            confidence=CONFIDENCE_L2,
            rationale=(
                f"市场已发布能力包命中（{match}）——复用既有实现，落 O1 工具实现面；"
                f"安装与启用走既有审核链（09 §13.3 ②「走既有审核」）"
            ),
            drafted_at=now.isoformat(),
        )


# ---------------------------------------------------------------- L3 LLM 起草（agent 环节，过校验）


# 受约束生成的固定 JSON Schema（complete_structured 契约：返回值已过 schema 校验）
L3_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action_iri": {"type": "string", "description": "行动类语义标注（必须取自种子行动类集）"},
        "execution_mode": {
            "type": "string",
            "enum": [mode.value for mode in ToolExecutionMode],
            "description": "工具实现形态声明",
        },
        "description": {"type": "string", "description": "候选工具描述（非空）"},
        "params_schema": {"type": "object", "description": "入参 JSON Schema 草案"},
        "steps": {"type": "array", "items": {"type": "string"}, "description": "实现步骤草案"},
    },
    "required": ["action_iri", "execution_mode", "description", "params_schema", "steps"],
    "additionalProperties": False,
}

_L3_SYSTEM_PROMPT = (
    "你是 ontology-agent 平台 ORSI 工具进化链路的 G1 起草器（architecture/09 §13.3 ③）。"
    "针对给定缺口场景起草一个候选工具实现草案。铁律：action_iri 必须从给定的种子行动类集中"
    "选取（无语义标注不上架）；execution_mode 只能取枚举值；description 非空；只输出符合"
    " JSON Schema 的结构化结果，不要编造种子集之外的行动类。"
)


def validate_llm_draft(raw: Any, seed_actions: frozenset[str]) -> list[str]:
    """L3 产物确定性校验（宪法推理分级：LLM 输出必须过校验；返回问题清单，空=通过）。

    校验项（任何越界=拒绝该次起草）：① action_iri 非空且 ∈ 种子行动类集；② execution_mode
    ∈ 枚举；③ description 非空；④ params_schema 为对象、steps 为非空字符串数组。
    """
    problems: list[str] = []
    if not isinstance(raw, Mapping):
        return [f"产物非对象: {type(raw).__name__}"]
    action_iri = raw.get("action_iri")
    if not isinstance(action_iri, str) or not action_iri.strip():
        problems.append("action_iri 缺失或为空（无语义标注不上架）")
    elif action_iri.strip() not in seed_actions:
        problems.append(f"action_iri 越界（不在种子行动类集内）: {action_iri.strip()}")
    try:
        ToolExecutionMode(raw.get("execution_mode"))
    except ValueError:
        problems.append(f"execution_mode 非法枚举值: {raw.get('execution_mode')!r}")
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append("description 缺失或为空")
    if not isinstance(raw.get("params_schema"), dict):
        problems.append("params_schema 缺失或非对象")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps or not all(isinstance(s, str) and s for s in steps):
        problems.append("steps 缺失、非字符串数组或为空")
    return problems


class L3LlmStep:
    """③ LLM 起草候选工具（本链唯一 agent 环节）：受约束生成 + 确定性校验，越界即拒。

    model 为 None = 整级跳过（如实降级，非失败）。重试预算：初始 1 次 + 重试 ≤
    ``L3_MAX_RETRIES``；端口异常与校验拒绝同耗预算但**分开定类**（``port_error``=模型渠道
    故障，``rejected``=模型有响应但越界被拒；预算全数为端口异常才定 ``port_error``，掺入
    任何校验拒绝即定 ``rejected``——校验拒绝是更根本的失败）。耗尽 → None（工单保持
    draft，零副作用）。

    异常捕获口径（kb_extraction 两树并集先例）：端口契约族 ``ModelPortError``（5xxx）∪
    ``RuntimeError`` 树（生产端口 OpenAICompatibleModelPort 实抛 ModelGatewayError 族，
    本层因依赖倒置不可 import 其名，按基类兜住）；AttributeError/TypeError 等编程错误
    不在捕获面，照常上抛响亮失败。
    """

    async def run(
        self,
        proposal: Proposal,
        *,
        action_iri: str,
        keywords: Iterable[str],
        seed_actions: frozenset[str],
        registry_iris: Iterable[str],
        model: ModelPort | None,
        now: datetime,
    ) -> tuple[DraftArtifact | None, str, str]:
        """返回 (草案 | None, 末级 outcome, 结论文本)；outcome ∈ skipped|port_error|rejected
        （命中时为 ``hit`` 语义由调用方落 attempts，本方法仍返回 ``hit`` 以便直用）。"""
        if model is None:
            return None, "skipped", "model 未注入——L3 整级跳过（如实降级，非失败）"
        gap = proposal.envelope.get("gap") or {}
        evidence = gap.get("evidence") or {}
        user_prompt = (
            f"缺口工单：kind={gap.get('kind')} / 簇规模={evidence.get('cluster_size')} / "
            f"窗口={evidence.get('window_days')} 天 / 指纹={str(evidence.get('fingerprint') or '')[:12]}\n"
            f"缺口行动类: {action_iri or '（无——unmapped_intent，请从种子行动类集中选定归处）'}\n"
            f"检索词元: {' '.join(keywords) or '—'}\n"
            f"失败模式: {(evidence.get('samples') or [{}])[0].get('failure_mode', '—')}\n"
            f"种子行动类集（action_iri 只能从中选取）:\n"
            + "\n".join(f"- {iri}" for iri in sorted(seed_actions))
            + f"\n平台已绑定行动类（避免重复起草）: {', '.join(sorted(registry_iris)) or '—'}"
        )
        total_budget = 1 + L3_MAX_RETRIES
        attempt_notes: list[str] = []
        port_error_count = 0
        for attempt in range(1, total_budget + 1):
            try:
                raw = await model.complete_structured(
                    system=_L3_SYSTEM_PROMPT,
                    user=user_prompt,
                    json_schema=L3_OUTPUT_SCHEMA,
                    trace_id=proposal.source_trace_ids[0] if proposal.source_trace_ids else None,
                )
            except (ModelPortError, RuntimeError) as exc:  # 两树并集（见类 docstring）；编程错误照常上抛
                port_error_count += 1
                attempt_notes.append(f"第 {attempt} 次端口异常（{type(exc).__name__}）: {exc}")
                continue
            problems = validate_llm_draft(raw, seed_actions)
            if not problems:
                draft = dict(raw)
                return (
                    DraftArtifact(
                        path=DraftPath.LLM,
                        surface=EvolutionSurface.TOOL_IMPL,  # 新实现绑定（§13.2 分界铁律）
                        content=draft,
                        action_iri=str(draft["action_iri"]).strip(),
                        execution_mode=ToolExecutionMode(draft["execution_mode"]),
                        confidence=CONFIDENCE_L3,
                        rationale=(
                            f"LLM 起草候选工具（第 {attempt} 次过确定性校验：action_iri 落种子集 / "
                            f"execution_mode 枚举合法 / description 非空）——落 O1 工具实现面，"
                            f"草案须过 G2 三级门禁（09 §13.3 ③）"
                        ),
                        drafted_at=now.isoformat(),
                    ),
                    "hit",
                    f"L3 第 {attempt} 次产物通过确定性校验（种子集 {len(seed_actions)} 个行动类；"
                    f"此前 {len(attempt_notes)} 次失败: {'; '.join(attempt_notes) or '—'}）",
                )
            attempt_notes.append(f"第 {attempt} 次产物被确定性校验拒绝: {'; '.join(problems)}")
        outcome = "port_error" if port_error_count == total_budget else "rejected"
        return (
            None,
            outcome,
            f"L3 重试耗尽（{total_budget} 次均失败，定类 {outcome}: {'; '.join(attempt_notes)}）——工单保持 draft",
        )


# ---------------------------------------------------------------- 三级串联（降路径语义）


async def draft_gap_proposal_detailed(
    proposal: Proposal,
    *,
    seed_actions: Iterable[str],
    registry_iris: Iterable[str] = (),
    market: MarketSearchPort | None = None,
    model: ModelPort | None = None,
    now: datetime | None = None,
) -> tuple[DraftArtifact | None, list[DraftAttempt]]:
    """三级降路径串联（L1→L2→L3 顺序，首次命中即返），返回 (草案 | None, 逐级尝试留痕)。

    命中即写回 ``proposal.envelope["draft_artifact"]``（JSONB 整体重赋值纪律：新字典整体
    替换 envelope，不做嵌套就地改写）；**失败零副作用**（envelope 原样、状态不动）；重跑
    同工单覆盖 draft_artifact（幂等）。候选状态**保持 draft**——迁移唯一入口在
    RsiService/Proposal.transition（G2 及以后本模块不做也不 stub）。
    """
    if proposal.trigger is not TriggerTrack.GAP:
        raise ValueError(f"非缺口轨工单（trigger={proposal.trigger.value}）不进 G1 起草（09 §13.3）")
    moment = now or datetime.now(UTC)
    seeds = frozenset(seed_actions)
    bound = frozenset(registry_iris)
    action_iri, keywords = gap_action_context(proposal)
    attempts: list[DraftAttempt] = []

    artifact = L1ComposeStep().run(proposal, action_iri=action_iri, seed_actions=seeds, registry_iris=bound, now=moment)
    if artifact is not None:
        attempts.append(DraftAttempt("L1", "hit", "同域既有行动类组合为计划模板（surface=O5）"))
    else:
        attempts.append(
            DraftAttempt(
                "L1",
                "miss",
                "无同域（同命名空间）既有行动类可组合" if action_iri else "工单无行动类锚点（unmapped_intent 型）",
            )
        )
        artifact, note = await L2MarketStep().run_market(
            proposal, action_iri=action_iri, keywords=keywords, market=market, now=moment
        )
        if artifact is not None:
            attempts.append(DraftAttempt("L2", "hit", note))
        else:
            attempts.append(DraftAttempt("L2", "miss", note))
            artifact, l3_outcome, note = await L3LlmStep().run(
                proposal,
                action_iri=action_iri,
                keywords=keywords,
                seed_actions=seeds,
                registry_iris=bound,
                model=model,
                now=moment,
            )
            if artifact is not None:
                attempts.append(DraftAttempt("L3", "hit", note))
            else:
                # outcome 由 L3 步自报：skipped（model 未注入）/ port_error（渠道故障）/
                # rejected（产物越界被拒）——报告与审计据此分辨故障类别（ocr 评审 ④）
                attempts.append(DraftAttempt("L3", l3_outcome, note))

    if artifact is not None:
        proposal.envelope = {**proposal.envelope, "draft_artifact": artifact.to_payload()}
    return artifact, attempts


async def draft_gap_proposal(
    proposal: Proposal,
    *,
    seed_actions: Iterable[str],
    registry_iris: Iterable[str] = (),
    market: MarketSearchPort | None = None,
    model: ModelPort | None = None,
    now: datetime | None = None,
) -> DraftArtifact | None:
    """G1 起草入口（三级降路径首次命中即返；语义与留痕口径见 ``draft_gap_proposal_detailed``）。

    状态红线（docstring 契约）：本入口只写 envelope 不迁状态——``proposal.status`` 恒保持
    draft（迁移唯一入口在 RsiService/Proposal.transition；G2 evaluating 起的评估属后续批次）。
    """
    artifact, _ = await draft_gap_proposal_detailed(
        proposal,
        seed_actions=seed_actions,
        registry_iris=registry_iris,
        market=market,
        model=model,
        now=now,
    )
    return artifact
