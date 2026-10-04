# tests/skills/test_skill_model.py
"""SkillEntry 聚合单测（docs/Agent/14 §2 市场件五态状态机）。"""

import uuid

import pytest

from services.platform.kernel import DomainError
from services.skills.domain.model.skill import SkillEntry, SkillOrigin, SkillStatus


def _entry(**kw) -> SkillEntry:
    base = dict(
        tenant_id=uuid.uuid4(),
        name="it-技能",
        source_uri="external://example/skill.md",
        version="1.0.0",
        description="测试技能",
        body_bytes=1024,
        origin=SkillOrigin.EXTERNAL,
    )
    base.update(kw)
    return SkillEntry(**base)


def test_五态枚举与存储字面量逐字一致():
    # 14 §2 状态机原文五态；ck_skills_assets_status 同词表
    assert {s.value for s in SkillStatus} == {"draft", "in_review", "listed", "deprecated", "revoked"}


def test_v1直通_登记默认listed():
    # Arrange & Act
    entry = _entry()
    # Assert：14 §2 v1 tools/skills 直通 listed（静态扫描+清单校验两门禁先行）
    assert entry.status is SkillStatus.LISTED


def test_全链合法迁移_draft到revoked():
    # Arrange
    entry = _entry(status=SkillStatus.DRAFT)
    # Act：全链 draft → in_review → listed → deprecated → revoked
    entry.submit()
    assert entry.status is SkillStatus.IN_REVIEW
    entry.approve()
    assert entry.status is SkillStatus.LISTED
    entry.delist()
    assert entry.status is SkillStatus.DEPRECATED
    entry.revoke()
    assert entry.status is SkillStatus.REVOKED


def test_下架后恢复回边合法():
    # Arrange
    entry = _entry()  # listed
    # Act
    entry.delist()
    entry.restore()
    # Assert：deprecated → listed 回边（恢复动作）
    assert entry.status is SkillStatus.LISTED


def test_非法迁移_draft直跳listed拒绝():
    # Arrange
    entry = _entry(status=SkillStatus.DRAFT)
    # Act / Assert：draft 不可跳过 in_review 直达 listed（终审挂接点预留）
    with pytest.raises(DomainError, match="4601"):
        entry.approve()


def test_非法迁移_in_review直跳deprecated拒绝():
    entry = _entry(status=SkillStatus.IN_REVIEW)
    with pytest.raises(DomainError, match="4601"):
        entry.delist()


def test_revoked终态再迁移拒绝():
    entry = _entry(status=SkillStatus.REVOKED)
    for 动作 in (entry.submit, entry.approve, entry.delist, entry.restore, entry.revoke):
        with pytest.raises(DomainError, match="4601"):
            动作()


def test_touch回写审计人():
    # Arrange
    entry = _entry()
    actor = uuid.uuid4()
    # Act
    entry.touch(actor)
    # Assert
    assert entry.updated_by == actor
    assert entry.updated_at is not None
