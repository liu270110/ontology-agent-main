"""rsi TBox v0.1 装载与贡献者/贡献产物 SHACL 校验（09 §13.5 + §14.1；批次 A 最小切片）。

制品：services/ontology/turtle/rsi.ttl（内建自进化本体，随平台版本发布；mem.ttl 同款
形态——TBox + SHACL shapes 一体）。复用点：core/tbox.load_turtle 装载（解析失败统一
ValueError）；core/shacl.validate 单条候选校验（按位置传 data_graph 规避本环境 pyshacl
双关键字静默不校验缺陷，同 mem_tbox 口径）。

v0.1 边界（09:173 交付前口径不变）：rsi:Candidate 生命周期状态迁移的运行期强制仍以
PG CHECK 枚举 + 代码校验为准（services/rsi/proposal.py Proposal 状态机）——本模块 SHACL
只承载批次 A 最小切片（贡献者身份三要素值域收口 + 贡献产物必挂贡献者/进化面、非法面拒绝）。
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

from services.ontology.core.shacl import validate as core_shacl_validate
from services.ontology.core.tbox import load_turtle

RSI = Namespace("https://ontology-agent.dev/ontology/rsi#")  # 与 rsi.ttl @prefix rsi 一致（09 §13.5/§14.1）
_TTL_PATH = Path(__file__).resolve().parent.parent / "turtle" / "rsi.ttl"
_CONTRIBUTOR_IRI = "https://ontology-agent.dev/instance/rsi-contributor"
_CONTRIBUTION_IRI = "https://ontology-agent.dev/instance/rsi-contribution"
# 封闭八面（surfaces.py §13.2 同源词汇表；SHACL sh:in 与此处常量一致）
SURFACE_VALUES: tuple[str, ...] = ("O1", "O2", "O3", "O4", "O5", "O6", "O7", "O8")
# 治理三档（宪法 3；09 §14.1 治理档位映射贡献权限）
GOVERNANCE_TIERS: tuple[str, ...] = ("solo", "team", "enterprise")


@lru_cache(maxsize=1)
def load_rsi_graph() -> Graph:
    """rsi.ttl → rdflib 内存图（TBox + SHACL shapes 一体；进程内缓存，随平台版本只读）。"""
    try:
        graph = load_turtle(_TTL_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:  # 文件缺失/编码错/Turtle 解析失败——带制品路径上下文重抛
        raise ValueError(f"rsi.ttl 装载失败（{_TTL_PATH}）：{e}") from e
    graph.bind("rsi", RSI)
    return graph


def validate_contributor(payload: dict[str, Any]) -> list[str]:
    """贡献者身份单条 SHACL 校验（复用 core/shacl 引擎入口）：违规清单（空=通过）。

    payload → 临时数据图（focus 节点固定 _CONTRIBUTOR_IRI）→ shapes = rsi.ttl：
    contributorId/displayName 非空恰一、governanceTier ∈ 三档（sh:in）、trustScore
    [0,1] decimal（初始 1.0）。payload 键：contributor_id/display_name/governance_tier/
    trust_score/delivery_dir（snake_case，与 API DTO 同词汇表）。
    """
    data = Graph()
    data.bind("rsi", RSI)
    subject = URIRef(_CONTRIBUTOR_IRI)
    data.add((subject, RDF.type, RSI.Contributor))
    if payload.get("contributor_id"):
        data.add((subject, RSI.contributorId, Literal(str(payload["contributor_id"]))))
    if payload.get("display_name"):
        data.add((subject, RSI.displayName, Literal(str(payload["display_name"]))))
    if payload.get("governance_tier"):
        data.add((subject, RSI.governanceTier, Literal(str(payload["governance_tier"]))))
    if payload.get("trust_score") is not None:
        # decimal 字面量（shape sh:datatype xsd:decimal；plain literal 会触发 datatype 违规）
        data.add((subject, RSI.trustScore, Literal(Decimal(str(payload["trust_score"])))))

    if payload.get("delivery_dir"):
        data.add((subject, RSI.deliveryDir, Literal(str(payload["delivery_dir"]))))
    report = core_shacl_validate(data, load_rsi_graph(), tbox_graph=load_rsi_graph(), focus_nodes=[subject])
    if report.conforms:
        return []
    return [v.message or v.constraint or "SHACL 违规" for v in report.results]


def validate_contribution(payload: dict[str, Any]) -> list[str]:
    """贡献产物单条 SHACL 校验（复用 core/shacl 引擎入口）：违规清单（空=通过）。

    批次 A 核心门禁（09 §14.5）：hasContributor 必挂（≥1 且指向 Contributor）、
    contributesSurface 必挂且 ∈ 封闭八面（非法面拒绝）。payload 键：has_contributor
    （贡献者 IRI）、contributes_surface（面编号）。v0.1 数据图内同时落 Contributor 节点
    使 sh:class 可满足——形态对齐 mem_tbox 单条候选校验（focus 节点固定）。
    """
    data = Graph()
    data.bind("rsi", RSI)
    subject = URIRef(_CONTRIBUTION_IRI)
    data.add((subject, RDF.type, RSI.Contribution))
    contributor = str(payload.get("has_contributor") or _CONTRIBUTOR_IRI)
    data.add((URIRef(contributor), RDF.type, RSI.Contributor))
    data.add((subject, RSI.hasContributor, URIRef(contributor)))
    if payload.get("contributes_surface"):
        data.add((subject, RSI.contributesSurface, Literal(str(payload["contributes_surface"]))))
    # focus 单点求值（E-4 K1-c 透传面）：只对贡献产物节点判违规——
    # 数据图内补的 Contributor 骨架节点（满足 sh:class）不参与 conforms
    report = core_shacl_validate(data, load_rsi_graph(), tbox_graph=load_rsi_graph(), focus_nodes=[subject])
    if report.conforms:
        return []
    return [v.message or v.constraint or "SHACL 违规" for v in report.results]
