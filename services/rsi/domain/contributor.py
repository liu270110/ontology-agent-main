"""ORSI 外部贡献者聚合（architecture/09 §14.1 贡献者协议；批次 A 竖切）。

身份三要素（§14.1 逐字）：contributor_id（claims 绑定与投递目录命名）/ 显示名 / 治理档位
（solo=自用工具即生效留痕 / team=租户审批 / enterprise=双审；宪法 3 三档，硬门禁任何档
不可跳过）；信誉分=转正率与回归失败率滑动窗（初始 1.0；滑动窗与自动降档属批次 C——
本批只承载初始值，不实现判定）。投递目录约定（§14.1）：每贡献者一个受沙箱约束的投递区
``.oa/contributors/<contributor-id>/{inbox,workspace,products}/``——本聚合只落**约定路径
记录**（字符串），不触真实文件系统（投递区沙箱投影随批次 B/C 接线）。

装配绑定（§14.3 步 4 热装配，隔离命名空间）：``contributor:<id>`` 命名空间的绑定集，
物理分表 rsi_contributor_bindings（contributor_id 外键+版本号+shadow 标记）——装配/升级/
回滚均为该命名空间内操作，既有任何 agent 的绑定零触碰；**一切装配默认 shadow**（§14.3
步 6 灰度：可调用但不进计划候选；批次 A 无转正路径）。

红线（§14.1 注册流 + §13 阶段 A 裁决继承）：注册≠生效——外部贡献只进候选池，apply 恒
拒绝红线不变；本聚合零进化副作用，不触 RsiService/gates/apply 路径（源断言见
tests/rsi/test_contributor.py）。

本模块零第三方依赖（stdlib + 本包 surfaces，L4 领域纯度同 domain/orsi.py 纪律）；
SHACL 门禁在业务层收口（domain 不引 rdflib）。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from services.rsi.surfaces import EvolutionSurface  # 封闭八面（09 §13.2，唯一本包依赖）

# ---------------------------------------------------------------- 值域（09 §14.1）

# 信誉分初始值（§14.1：转正率与回归失败率滑动窗；批次 C 实装滑动窗与降档判定）
DEFAULT_TRUST_SCORE = Decimal("1.0")

# contributor_id slug 规则（投递目录/命名空间/claims 绑定的公共命名面；MCP target 名
# _NAME_RE 同族收紧：小写开头、小写字母数字连字符、≤64）
CONTRIBUTOR_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

# 投递目录约定根（§14.1 投递目录约定：贡献者只见自己的沙箱投影，平台工作区隔离红线沿 08 篇）
DELIVERY_DIR_TEMPLATE = ".oa/contributors/{contributor_id}"
DELIVERY_SUBDIRS: tuple[str, ...] = ("inbox", "workspace", "products")  # inbox=平台投他 / products=他投平台

# 装配命名空间前缀（§14.3 步 4：与既有 capability_bindings 物理分表分键，零触碰）
BINDING_NAMESPACE_PREFIX = "contributor:"


class GovernanceTier(StrEnum):
    """治理档位（宪法 3 三档；§14.1 治理档位映射贡献权限；硬门禁任何档不可跳过）。"""

    SOLO = "solo"  # 自用工具即生效留痕
    TEAM = "team"  # 租户审批
    ENTERPRISE = "enterprise"  # 双审


# ---------------------------------------------------------------- 错误（orsi.py 同款口径：不触 02 §7 码表）


class ContributorError(Exception):
    """贡献者聚合错误基类（v1 无独立错误码段——不触 02 §7 码表，OrsiCapabilityError 同款口径）。"""


class ContributorNotFound(ContributorError):
    """贡献者不存在（含跨租户探测——一律「不存在」，不泄露存在性）。"""


class DuplicateContributorId(ContributorError):
    """同 contributor_id 已注册（rsi_contributors.contributor_id 全局唯一约束语义）。"""


class ContributorRejected(ContributorError):
    """贡献者注册被门禁拒绝（SHACL 形状违规等——拒绝即返回违规清单，不静默）。"""


class AssemblyIntrusionError(ContributorError):
    """装配事故：绑定快照 diff 非空（既有绑定被触碰）——§14.3 步 5 无侵扰断言机械拒绝。"""


# ---------------------------------------------------------------- 聚合：贡献者


@dataclass(slots=True)
class RsiContributor:
    """外部贡献者（09 §14.1 实体；PG 行的领域形）。

    SHACL 门禁（rsi:ContributorShape）在业务层收口——领域层只做 slug 形构与档位值域
    两项机械校验（零 rdflib 依赖，L4 纯度）；trust_score 批次 A 只承载初始值 1.0。
    """

    tenant_id: uuid.UUID
    contributor_id: str  # 全局唯一 slug（投递目录/claims/命名空间公共命名面）
    display_name: str
    governance_tier: GovernanceTier = GovernanceTier.TEAM
    trust_score: Decimal = DEFAULT_TRUST_SCORE  # 初始 1.0（批次 C 前无滑动窗判定）
    delivery_dir: str = ""  # 投递目录约定路径记录（注册流生成；不触真实 FS）
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    probe_profile: dict[str, Any] | None = None  # 最近一次探测握手能力面档案（§14.3 步 1；None=未探测）
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    deleted_at: datetime | None = None  # 软删列（v1 无删除端点，列随表落）

    def __post_init__(self) -> None:
        if not isinstance(self.governance_tier, GovernanceTier):
            raise ContributorError(f"governance_tier 必须为 solo|team|enterprise，收到: {self.governance_tier!r}")
        if not self.contributor_id or not CONTRIBUTOR_ID_RE.match(self.contributor_id):
            raise ContributorError(
                f"contributor_id 非法（{CONTRIBUTOR_ID_RE.pattern}）: {self.contributor_id!r}"
                "（投递目录/claims 绑定公共命名面）"
            )
        if not self.display_name or not self.display_name.strip():
            raise ContributorError("display_name 不得为空")
        if self.trust_score < 0 or self.trust_score > 1:
            raise ContributorError(f"trust_score 须在 [0,1]（初始 {DEFAULT_TRUST_SCORE}），收到: {self.trust_score}")

    @property
    def namespace(self) -> str:
        """装配命名空间名（§14.3 步 4：``contributor:<id>``）。"""
        return f"{BINDING_NAMESPACE_PREFIX}{self.contributor_id}"


def build_delivery_dir(contributor_id: str) -> str:
    """投递目录约定路径记录（§14.1；仅字符串记录，真实目录随沙箱投影批次接线）。"""
    if not CONTRIBUTOR_ID_RE.match(contributor_id):
        raise ContributorError(f"contributor_id 非法，拒绝生成投递目录: {contributor_id!r}")
    return DELIVERY_DIR_TEMPLATE.format(contributor_id=contributor_id)


# ---------------------------------------------------------------- 聚合：装配绑定


@dataclass(slots=True)
class ContributorBinding:
    """贡献者命名空间绑定行（09 §14.3 步 4/6；rsi_contributor_bindings 行的领域形）。

    版本化（§14.3 步 4）：每次装配产版本号（命名空间内自增），一键回滚=克隆目标版本为
    新版本行（append-only，全程可追溯宪法 5）；**shadow 恒缺省 True**（步 6 灰度：
    可调用但不进计划候选；批次 A 无转正路径——转正=灰度 N 次成功，随批次 B/C）。
    current 语义：同 (contributor_id, surface) 下 version 最大者为当前生效登记（shadow）。
    """

    tenant_id: uuid.UUID
    contributor_id: str  # 外键 → rsi_contributors.contributor_id（命名空间主名）
    surface: EvolutionSurface  # 挂靠进化面（封闭八面，09 §13.2）
    version: int  # 命名空间内版本号（1 起；装配/升级/回滚均产新版本行）
    payload: dict[str, Any] = field(default_factory=dict)  # 装配载荷（能力面引用/适配产物引用）
    shadow: bool = True  # 灰度标记（§14.3 步 6；批次 A 恒 True——转正路径不存在即恒 shadow）
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not isinstance(self.surface, EvolutionSurface):
            raise ContributorError(f"surface 必须为八大进化面枚举（09 §13.2），收到: {self.surface!r}")
        if self.version < 1:
            raise ContributorError(f"version 须 ≥1（命名空间内自增），收到: {self.version}")
        if not self.shadow:
            # 红线：一切装配默认 shadow（§14.3 步 6）；批次 A 无灰度转正面，显式非 shadow 即拒绝
            raise ContributorError("shadow=False 不可直达（装配恒 shadow，转正随灰度批次接线，09 §14.3 步 6）")


# ---------------------------------------------------------------- 仓储过滤（domain/repo 同款形态）


@dataclass(frozen=True, slots=True)
class ContributorFilter:
    """贡献者列表过滤（v1：分页即可；档位过滤随批次 C 信誉分管理面）。"""

    offset: int = 0
    limit: int = 20


@dataclass(frozen=True, slots=True)
class BindingFilter:
    """绑定行过滤（contributor_id/surface/至多到版；无侵扰快照用全量 snapshot）。"""

    contributor_id: str | None = None
    surface: EvolutionSurface | None = None
    offset: int = 0
    limit: int = 100
