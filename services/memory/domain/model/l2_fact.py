"""L2 结构化记忆领域模型（docs/memory/多层记忆设计.md §7 数据模型；§5.4 幂等/失效语义）。

与 memory.py 的四层 MemoryRecord（架构设计/06 规格，M4 memory_records 表）并存：
本模型只承载 M3 过渡方案的结构化 L2 用户事实（PG memory_l2_facts，database/01 §3.7）。
纪律同 memory.py：validate_assignment=True；状态只经聚合方法流转（白名单）；
全程不物理删除——superseded/invalidated 均为墓碑留痕。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")
_FINGERPRINT_HEX_LEN: Final[int] = 64  # sha256 hex 长度（列 fingerprint VARCHAR(64)）


class FactCategory(StrEnum):
    """memory §7 category 枚举（画像/偏好/事实/技能笔记）。"""

    PROFILE = "profile"
    PREFERENCE = "preference"
    FACT = "fact"
    SKILL_NOTE = "skill_note"


class FactStatus(StrEnum):
    """memory §7 status 枚举（无物理删除：superseded/invalidated 均为墓碑留痕）。"""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


_VALID_TRANSITIONS: Final[dict[FactStatus, set[FactStatus]]] = {
    FactStatus.ACTIVE: {FactStatus.SUPERSEDED, FactStatus.INVALIDATED},
    FactStatus.SUPERSEDED: set(),
    FactStatus.INVALIDATED: set(),
}


class FactStateError(Exception):
    """非法状态流转（网关映射 4xxx 业务错误）。"""


def normalize_content(content: str) -> str:
    """事实规范化文本：去首尾空白 + 内部空白折叠为单空格（memory §5.4 指纹判重输入）。"""
    return _WHITESPACE.sub(" ", content.strip())


def fact_fingerprint(content: str, category: FactCategory) -> str:
    """事实指纹 = sha256(规范化文本 + "|" + category)（memory §5.4：判重后再走 ADD/UPDATE）。

    墓碑永久抑制语义（P3-3 文档化，2026-09-27）：指纹判重不过滤 status——invalidated/
    superseded 事实的同指纹仍占据判重位，同指纹再写入被**永久抑制**（防复活：失效/被取代的
    事实不得因重放/重提交而复活；更正须走新事实 + supersedes 版本链，repo 查询注释同口径）。
    近重复边界：normalize_content 仅折叠空白，标点/全半角/大小写差异产生不同指纹——
    判重不合并，依赖「LLM 抽取+人工终审」上游消化（阈值随 memory §10 标定一并裁量）。
    """
    normalized = f"{normalize_content(content)}|{category.value}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class L2Fact(BaseModel):
    """L2 用户级记忆事实（memory §7 字段全集；指纹构造期自动计算并随内容变更重算）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    agent_id: uuid.UUID | None = None  # 产生来源 agent（指针不设 FK，同 tasks.active_run_id 口径）
    content: str = Field(min_length=1)
    category: FactCategory
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    decay_score: float = Field(default=0.0, ge=0.0)  # 时间衰减分（写入时=confidence；定期重算）
    embedding_ref: str | None = Field(default=None, max_length=64)  # Milvus/pgvector 主键占位（M3 过渡）
    source_session_id: uuid.UUID | None = None
    source_message_ids: list[uuid.UUID] = Field(default_factory=list)
    status: FactStatus = FactStatus.ACTIVE
    supersedes_id: uuid.UUID | None = None  # 版本链（不设 FK 防自引用锁，同 database/01 §3.11 口径）
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    fingerprint: str = Field(default="", min_length=0, max_length=_FINGERPRINT_HEX_LEN)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _sync_fingerprint(self) -> L2Fact:
        """指纹恒等于 规范化文本+category 的哈希（§5.4 幂等判重的唯一事实源）。

        守卫防递归：validate_assignment 会让赋值重入本校验器，仅在指纹漂移时回写。
        """
        expected = fact_fingerprint(self.content, self.category)
        if self.fingerprint != expected:
            self.fingerprint = expected
        return self

    def _transition(self, target: FactStatus, now: datetime) -> None:
        if target not in _VALID_TRANSITIONS[self.status]:
            raise FactStateError(f"{self.status} -> {target} 不合法")
        self.status = target
        self.updated_at = now

    def invalidate(self, now: datetime, *, reason: str) -> None:
        """失效标记（墓碑式软删，api/01 §5.5：无 DELETE 端点；同时写 valid_to 双时间线）。

        reason 必填（K2-a §11.1：无 reason 拒绝失效）——理由本身落失效影子表
        （memory_l2_fact_invalidations，与 save_state 同事务），主表不加列（迁移只增不改，
        影子行承载完整归档语义）。
        """
        if not (reason and reason.strip()):
            raise ValueError("失效必须携带 reason（§11.1：无 reason 拒绝失效）")
        self._transition(FactStatus.INVALIDATED, now)
        self.valid_to = now

    def supersede(self, by_id: uuid.UUID, now: datetime) -> None:
        """新事实取代旧事实（memory §2 UPDATE 动作；旧版本置 superseded 不物理删）。"""
        self._transition(FactStatus.SUPERSEDED, now)
        self.supersedes_id = by_id

    def decay_score_at(self, now: datetime, half_life_days: float) -> float:
        """confidence × 时间衰减（memory §3 加权合并：半衰期默认 30 天，config 可调）。"""
        age_days = max((now - self.created_at).total_seconds(), 0.0) / 86400.0
        return float(self.confidence * 0.5 ** (age_days / half_life_days))


class FactInvalidation(BaseModel):
    """失效影子行领域对象（K2-a §11.1 hindsight 范式：失效即归档，行不可变、仅 restored_at 可回填）。

    restore 语义红线：**影子层可见性恢复，非复活**——主表 fact 保持 INVALIDATED 终态不动
    （P3-3 防复活：失效事实不得复活，同指纹再写入仍被永久抑制）；本对象只承载归档行自身的
    恢复标记，全程审计留痕归 repo/api 层。
    """

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    fact_id: uuid.UUID
    user_id: uuid.UUID
    content: str = Field(min_length=1)  # 失效时事实文本快照（主表后续 UPDATE 不影响追溯）
    reason: str = Field(min_length=1)  # 必填（§11.1）
    invalidated_at: datetime
    restored_at: datetime | None = None

    @property
    def active(self) -> bool:
        """失效是否生效中（restored_at 为空 = 生效中）。"""
        return self.restored_at is None

    def restore(self, now: datetime) -> None:
        """影子行 restored_at 回填（幂等：已恢复原样不动；先恢复后失效的时间倒置拒绝）。"""
        if self.restored_at is not None:
            return
        if now < self.invalidated_at:
            raise FactStateError(f"restored_at({now}) 不得早于 invalidated_at({self.invalidated_at})")
        self.restored_at = now
