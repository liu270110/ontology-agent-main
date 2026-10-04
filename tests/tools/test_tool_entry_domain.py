"""ToolEntry 领域单测：硬编码迁移表全边校验（零依赖；docs/Agent/14 §2 状态机）。"""

from __future__ import annotations

import uuid

import pytest

from services.platform.kernel import DomainError
from services.tools.domain.model.tool_entry import SourceChannel, ToolEntry, ToolStatus


def _entry(status: ToolStatus = ToolStatus.DRAFT) -> ToolEntry:
    return ToolEntry(
        tenant_id=uuid.uuid4(),
        name="it.tool.entry",
        action_iri="https://ontology.example/action/ItProbe",
        source_channel=SourceChannel.L1,
        semantic_annotation={"action_iri": "https://ontology.example/action/ItProbe"},
        version="1.0.0",
        status=status,
    )


def test_直通边_draft到listed_清单过即上架():
    # Arrange
    entry = _entry(ToolStatus.DRAFT)
    # Act
    entry.apply_transition(ToolStatus.LISTED)
    # Assert：v1 直通边（14 §2 清单校验+静态扫描先行）
    assert entry.status is ToolStatus.LISTED


def test_全链_draft到in_review到listed_人工链预留合法():
    # Arrange
    entry = _entry(ToolStatus.DRAFT)
    # Act
    entry.apply_transition(ToolStatus.IN_REVIEW)
    entry.apply_transition(ToolStatus.LISTED)
    # Assert：预留人工审核链可达
    assert entry.status is ToolStatus.LISTED


def test_驳回回边_in_review到draft_合法():
    # Arrange
    entry = _entry(ToolStatus.IN_REVIEW)
    # Act / Assert
    entry.apply_transition(ToolStatus.DRAFT)
    assert entry.status is ToolStatus.DRAFT


def test_下架恢复回边_listed到deprecated到listed_合法():
    # Arrange
    entry = _entry(ToolStatus.LISTED)
    # Act
    entry.apply_transition(ToolStatus.DEPRECATED)
    entry.apply_transition(ToolStatus.LISTED)
    # Assert：14 §2「恢复」回边
    assert entry.status is ToolStatus.LISTED


def test_撤销_listed与deprecated均可到revoked():
    # Arrange
    listed = _entry(ToolStatus.LISTED)
    deprecated = _entry(ToolStatus.DEPRECATED)
    # Act
    listed.apply_transition(ToolStatus.REVOKED)
    deprecated.apply_transition(ToolStatus.REVOKED)
    # Assert
    assert listed.status is ToolStatus.REVOKED and deprecated.status is ToolStatus.REVOKED


def test_非法迁移_revoked终态_任何出边拒绝():
    # Arrange
    entry = _entry(ToolStatus.REVOKED)
    # Act / Assert
    with pytest.raises(DomainError) as exc:
        entry.apply_transition(ToolStatus.LISTED)
    assert "4603" in str(exc.value)


def test_非法迁移_draft直跳deprecated与revoked_拒绝():
    # Arrange
    for target in (ToolStatus.DEPRECATED, ToolStatus.REVOKED):
        entry = _entry(ToolStatus.DRAFT)
        # Act / Assert
        with pytest.raises(DomainError):
            entry.apply_transition(target)


def test_非法迁移_in_review直跳deprecated_拒绝():
    # Arrange
    entry = _entry(ToolStatus.IN_REVIEW)
    # Act / Assert
    with pytest.raises(DomainError):
        entry.apply_transition(ToolStatus.DEPRECATED)


def test_非法迁移_listed自迁移与回draft_拒绝():
    # Arrange
    for target in (ToolStatus.LISTED, ToolStatus.DRAFT, ToolStatus.IN_REVIEW):
        entry = _entry(ToolStatus.LISTED)
        # Act / Assert
        with pytest.raises(DomainError):
            entry.apply_transition(target)
