"""ORSI 原子能力聚合（docs/Agent/14 §4 + architecture/09 §13.2/§13.5；M4.6-S3 注册表竖切）。

八大进化面枚举（face 字段值域）**复用** ``services.rsi.surfaces.EvolutionSurface``——
B9 G0 批已按 09 §13.2「进化面矩阵（八大组件）」实文落定封闭八面（出处小节=09 §13.2；
Agent14 §4 草拟名 context/retrieval/... 与实文不符，按实文校准，08 §13.5 同款收编）：
O1 工具实现 / O2 行动类语义 / O3 提示词 / O4 技能 / O5 控制流程 / O6 记忆策略 /
O7 检索策略 / O8 模型权重。封闭注册表：新增面=代码变更=人工审批（§13.2 分界铁律）。

红线（docs/Agent/14 §4「红线继承」+ 09 §1 宪法 3「候选非成品」机械执行点）：

1. **任何写操作不触发进化副作用**：注册/改状态只落登记行与审计行，不触
   ``RsiService``/``GapCollector``/门禁链/``apply`` 路径（源断言见
   tests/rsi/test_orsi_registry.py）——注册表只登记与检索；
2. **status→promoted 仅当 review 工单引用存在**：v1 无工单挂接面=恒不可迁，
   ``OrsiCapability.promote`` 无论是否携带工单引用一律抛
   ``OrsiPromotionBlockedError``（挂接点=M5+ review_workflow
   target_type=orsi_capability，随审核工作流批次接线后在此替换为工单存在性校验）。

promotion_evidence（17 篇 §3.3「版本迭代质量依据」，C 批 orsi_link v2 挂接）：
可选结构化证据（URI+version_diff 摘要五键，键集见 ``PROMOTION_EVIDENCE_KEYS``）——
**仅承载，不激活任何迁移**：promote() 红线原样恒拒；本字段是 M5+ 审查工单
（review_workflow target_type=orsi_capability）的**必填证据挂接点**——工单面接线后，
promote 校验「工单存在且其引用的 version_diff trend 不劣化」即读本字段（对应 suite 的
version_diff 即两代能力的质量证据链）。挂接点前该字段对注册表仅是登记元数据，
与 source_channel 同类：不作授权依据、不触发任何迁移。

本模块零第三方依赖（stdlib + 本包 surfaces），L4 领域纯度同款纪律。
"""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from services.rsi.surfaces import EvolutionSurface  # noqa: F401  (re-export 供 domain 消费方统一入口)

# ---------------------------------------------------------------- 值域枚举（Agent14 §4 逐字）


class GapFaceTrack(StrEnum):
    """缺口轨分级（source_face_track；Agent14 §4：normal/shortgap/critical）。

    v1 仅作登记元数据，不做自动判定/升级（缺口轨自动判定属 Agent14 §8 明确不做，
    随缺口轨巡检批次接入）。
    """

    NORMAL = "normal"  # 普通缺口
    SHORTGAP = "shortgap"  # 短期缺口（窗口内未补齐的跟进档）
    CRITICAL = "critical"  # 关键缺口


class SourceChannel(StrEnum):
    """能力来源通道（source_channel ∈ L0~L3；06 篇 §1.5 能力准入阶梯 + Agent14 §4）。

    纯来源元数据：不作授权依据（MCP annotations 同款红线，08 §2.3）、不触发任何通道迁移。
    """

    L0 = "L0"  # 平台工具面（tool bindings，docs/Agent/06 §1.5「L0 工具」）
    L1 = "L1"  # 技能（SKILL.md 资产，06 篇「L1 技能」）
    L2 = "L2"  # 能力包（市场包检索安装，06 篇「L2 包」；09 §13.3 G1-② 同源）
    L3 = "L3"  # 内置扩展（内核内置扩展/execution backends，06 篇「L3 内置扩展」）


class OrsiCapabilityStatus(StrEnum):
    """能力状态（Agent14 §4：nominal|candidate|promoted）。

    迁移语义（v1）：注册落 nominal/candidate（promoted 不可注册直达）；candidate→promoted
    仅经 ``OrsiCapability.promote``（红线 2：v1 恒不可迁）。
    """

    NOMINAL = "nominal"  # 常规在册（已在服役的能力登记）
    CANDIDATE = "candidate"  # 候选（候选非成品宪法 3：仅标记，晋升必经人工审核工单）
    PROMOTED = "promoted"  # 已晋升（经审核工单终审；v1 恒不可达）


# ---------------------------------------------------------------- 证据 payload 口径（17 篇 §3.1/§3.3）

# promotion_evidence 键集（17 篇 §3.1 注册 payload 五字段；闭集——缺键/多键皆拒）：
# - eval_tag         本次评测 tag（版本迭代曲线锚点，如 v0.2.0-m4.7）；
# - scenario_hash    场景集哈希（场景集变=指纹变=能力需重评，orsi_link 基准侧同源算法）；
# - metrics_digest   本次指标快照摘要（sha256，评审可比对依据）；
# - baseline_digest  上一 tag 同场景集的指标摘要（首轮 None——曲线首点无基线）；
# - version_diff_uri version_diff 产物路径（B 批 diff.py 产出；M5+ 工单审查的必读证据）。
PROMOTION_EVIDENCE_KEYS: tuple[str, ...] = (
    "eval_tag",
    "scenario_hash",
    "metrics_digest",
    "baseline_digest",
    "version_diff_uri",
)
# 必填非空键（eval_tag=版本迭代曲线锚点；version_diff_uri 首轮基线可 None——
# 无上一 tag 即无 diff 产物，M5+ 工单面对无 diff 的晋升可要求补跑基线后受理）
PROMOTION_EVIDENCE_REQUIRED: tuple[str, ...] = ("eval_tag",)


def validate_promotion_evidence(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """promotion_evidence 值域校验（构造期收口；非法即抛 OrsiCapabilityError 不静默）。

    规则：None=未挂证据（合法）；dict 且键集恰为 ``PROMOTION_EVIDENCE_KEYS``（闭集，
    API DTO extra="forbid" 同词汇表）；必填键值非空字符串，其余键值 str|None。
    返回规范化后的原 dict（就地校验语义，不改写内容）。
    """
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise OrsiCapabilityError(f"promotion_evidence 必须为 dict 或 None，收到: {type(payload).__name__}")
    keys = set(payload)
    expected = set(PROMOTION_EVIDENCE_KEYS)
    missing = expected - keys
    extra = keys - expected
    if missing or extra:
        raise OrsiCapabilityError(
            f"promotion_evidence 键集不符（五键闭集 {sorted(expected)}）："
            f"缺 {sorted(missing)} 多 {sorted(extra)}（17 篇 §3.1 payload 口径）"
        )
    for key in PROMOTION_EVIDENCE_KEYS:
        value = payload[key]
        if key in PROMOTION_EVIDENCE_REQUIRED:
            if not isinstance(value, str) or not value.strip():
                raise OrsiCapabilityError(f"promotion_evidence.{key} 必填且须非空字符串，收到: {value!r}")
        elif value is not None and not isinstance(value, str):
            raise OrsiCapabilityError(f"promotion_evidence.{key} 须为 str 或 None，收到: {value!r}")
    return payload


# ---------------------------------------------------------------- 错误


class OrsiCapabilityError(Exception):
    """ORSI 能力聚合错误基类（v1 无独立错误码段——不触 02 §7 码表，ProposalError 同款口径）。"""


class OrsiCapabilityNotFound(OrsiCapabilityError):
    """能力不存在（含跨租户探测——一律「不存在」，不泄露存在性）。"""


class OrsiDuplicateFingerprint(OrsiCapabilityError):
    """同指纹能力已注册（(tenant_id, face, capability_fingerprint) 唯一约束语义）。"""


class OrsiPromotionBlocked(OrsiCapabilityError):
    """晋升被红线拒绝（v1 恒拒：无 review 工单挂接面，Agent14 §4 红线继承）。"""


# ---------------------------------------------------------------- 指纹口径（canonical 规则 v1）

# 指纹口径版本（参与哈希输入；升级口径即换版本号，新旧指纹空间隔离——gap.py 同款纪律）
FINGERPRINT_VERSION = "orsi_cap_fp_v1"


def normalize_name(name: str) -> str:
    """name 规范化 v1：NFKC → casefold → 去首尾空白 → 连续空白折叠（gap.py `_norm_key` 同口径）。

    同一能力的书写格式差异（大小写/全半角/空白）不产生新指纹。
    """
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def capability_fingerprint(
    *,
    face: EvolutionSurface,
    name: str,
    source_channel: SourceChannel,
    version: str,
) -> str:
    """能力指纹 = 语义标注 canonical 序列化 sha256（口径 v1）。

    **canonical 规则（本 docstring 即唯一权威，升级即换 ``FINGERPRINT_VERSION``）**::

        canonical = "\\x1f".join([FINGERPRINT_VERSION, face.value, normalize_name(name),
                                  source_channel.value, version.strip()])
        capability_fingerprint = sha256(canonical.encode("utf-8")).hexdigest()

    - 语义标注四元组 = (进化面, 规范化能力名, 来源通道, 版本)：同一进化面内同源同版本同名
      即同能力（重复注册被 (tenant_id, face, fingerprint) 唯一约束拒绝）；
    - 全部输入均为 ``orsi_capabilities`` 落库列——指纹可独立复算校验（全程可追溯宪法 5）；
    - 分隔符 ``\\x1f``（单元分隔符）与 gap.py 场景指纹同款，防字段拼接歧义。
    """
    canonical = "\x1f".join(
        [FINGERPRINT_VERSION, face.value, normalize_name(name), source_channel.value, version.strip()]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 聚合


@dataclass(slots=True)
class OrsiCapability:
    """ORSI 原子能力（Agent14 §4 实体逐字段；PG 行的领域形）。

    ``capability_fingerprint`` 构造期自动计算（口径见 ``capability_fingerprint``）；
    显式传入仅限重放校验（与口径不符即抛错，不静默混算——GapEvent 同款纪律）。
    """

    tenant_id: uuid.UUID
    face: EvolutionSurface  # 八大进化面（封闭八面，09 §13.2）
    name: str  # 能力名（指纹输入；规范化口径见 normalize_name）
    version: str  # 版本列（Agent14 §5「版本列」；指纹输入）
    source_channel: SourceChannel  # 来源通道 L0~L3（纯元数据）
    source_face_track: GapFaceTrack = GapFaceTrack.NORMAL  # 缺口轨分级（v1 仅登记）
    status: OrsiCapabilityStatus = OrsiCapabilityStatus.CANDIDATE  # 缺省候选（宪法 3）
    evidence_uri: str | None = None  # 出处（可追溯证据；可为空——L0 内置登记可无外部出处）
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    capability_fingerprint: str = ""  # 构造期自动计算；显式传入仅限重放校验
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    deleted_at: datetime | None = None  # 软删列（Agent14 §5；v1 无删除端点，列随表落）
    promotion_evidence: dict[str, Any] | None = None  # 晋升证据挂接点（17 篇 §3.3；仅承载不激活迁移——见模块 docstring）

    def __post_init__(self) -> None:
        if not isinstance(self.face, EvolutionSurface):
            raise OrsiCapabilityError(f"face 必须为八大进化面枚举（09 §13.2），收到: {self.face!r}")
        if not isinstance(self.source_channel, SourceChannel):
            raise OrsiCapabilityError(f"source_channel 必须为 L0~L3 枚举，收到: {self.source_channel!r}")
        if not isinstance(self.source_face_track, GapFaceTrack):
            raise OrsiCapabilityError(
                f"source_face_track 必须为 normal|shortgap|critical，收到: {self.source_face_track!r}"
            )
        if not isinstance(self.status, OrsiCapabilityStatus):
            raise OrsiCapabilityError(f"status 必须为 nominal|candidate|promoted，收到: {self.status!r}")
        if not self.name or not self.name.strip():
            raise OrsiCapabilityError("name 不得为空")
        if not self.version or not self.version.strip():
            raise OrsiCapabilityError("version 不得为空")
        if self.status is OrsiCapabilityStatus.PROMOTED:
            # 红线 2 的注册侧半边：promoted 不可直达（含构造期），晋升仅经 promote()——v1 亦恒拒
            raise OrsiPromotionBlocked("promoted 不可注册直达（status→promoted 必经 review 工单迁移位，Agent14 §4）")
        validate_promotion_evidence(self.promotion_evidence)  # 17 篇 §3：五键闭集+必填键非空
        computed = capability_fingerprint(
            face=self.face, name=self.name, source_channel=self.source_channel, version=self.version
        )
        if self.capability_fingerprint and self.capability_fingerprint != computed:
            raise OrsiCapabilityError("capability_fingerprint 与规范口径不符（重放校验失败；一般应留空自动计算）")
        if not self.capability_fingerprint:
            self.capability_fingerprint = computed

    # ------------------------------------------------------------- 红线：晋升迁移位

    def promote(self, *, review_ticket_id: str | None = None) -> None:
        """候选晋升（status=candidate→promoted 唯一迁移口；**v1 恒拒绝**，红线 2）。

        Agent14 §4：「候选能力 status=candidate 仅标记，晋升 promoted 需人工审核工单
        （挂 review 域，v1 只留挂接点）」。v1 无工单挂接面 ⇒ 无论是否携带
        ``review_ticket_id`` 一律抛 ``OrsiPromotionBlockedError``，状态不变。

        **挂接点（M5+ 审核工作流批次替换本实现）**：此处改为校验 review 工单存在且有效
        （open/approved 工单 target_type=orsi_capability 且 target_id=本能力 id），校验过
        方置 status=promoted；迁移留痕由服务层审计承接（09 §6 红线 7）。届时工单审查的
        必填证据=本聚合 ``promotion_evidence``（version_diff trend 不劣化，17 篇 §3.3）——
        本批仅落字段承载，本实现不消费该字段（红线不动）。
        """
        _ = review_ticket_id  # v1 不消费：无工单挂接面，携带引用亦不可自证（防伪造工单号绕过）
        raise OrsiPromotionBlocked(
            "ORSI 晋升恒拒绝（v1）：status→promoted 需 review 工单引用，"
            "工单挂接面（review_workflow target_type=orsi_capability）随 M5+ 审核工作流批次接线"
        )
