# tests/skills/test_required_secrets_decl.py
"""K5 G-6 缩减版门 1 声明单测（方案依据=docs/Agent/13 §10；上游=deer-flow §10 frontmatter 声明门）。

覆盖：required-secrets 单行逗号分隔标量解析（含声明/不含或空/格式错三态）、
扫描投影 ScannedAsset.required_secrets、聚合面非法凭证名 fail-closed。
"""

import uuid

import pytest
from pydantic import ValidationError

from services.skills.business.scanner import (
    parse_frontmatter,
    parse_required_secrets,
    scan_repo_assets,
)
from services.skills.domain.model.skill import SkillEntry


def test_门1_声明含required_secrets_解析为凭证名元组():
    # Arrange：单行逗号分隔标量（scanner 零依赖解析风格；带引号整字段先剥）
    text = '---\nname: gh-publisher\ndescription: d\nrequired-secrets: "GITHUB_TOKEN, DEEPSEEK_API_KEY"\n---\nbody\n'
    # Act
    fields = parse_frontmatter(text)
    # Assert：整字段引号剥离 + 逗号拆分 + 逐项剥空白
    assert parse_required_secrets(fields.get("required-secrets")) == ("GITHUB_TOKEN", "DEEPSEEK_API_KEY")


def test_门1_声明不含或为空_得空集():
    # Assert：缺省键 / 空串 / 纯分隔符均得空集（空/缺省=空集口径）
    assert parse_frontmatter("---\nname: s\n---\n").get("required-secrets") is None
    assert parse_required_secrets(None) == ()
    assert parse_required_secrets("") == ()
    assert parse_required_secrets(" , , ") == ()


def test_门1_格式错_非法名丢弃告警_空项跳过_去重保序(caplog):
    # Arrange：合法+非法（连字符/数字开头）+空项+重复混排
    import logging

    with caplog.at_level(logging.WARNING):
        # Act
        out = parse_required_secrets("GITHUB_TOKEN, bad-name, , 9LEAD, GITHUB_TOKEN")
    # Assert：仅合法名保留（去重保序）；非法名丢弃且告警
    assert out == ("GITHUB_TOKEN",)
    assert sum("非法凭证名" in r.message for r in caplog.records) == 2  # bad-name 与 9LEAD


def test_门1_扫描投影_declared与非declared资产(tmp_path):
    # Arrange：一枚带声明、一枚不带
    root = tmp_path / "assets"
    (root / "with-secrets").mkdir(parents=True)
    (root / "with-secrets" / "SKILL.md").write_text(
        "---\nname: with-secrets\ndescription: d\nrequired-secrets: GITHUB_TOKEN, GH_TOKEN\n---\nbody",
        encoding="utf-8",
    )
    (root / "plain").mkdir()
    (root / "plain" / "SKILL.md").write_text("---\nname: plain\ndescription: d\n---\nbody", encoding="utf-8")
    # Act
    assets = {a.name: a for a in scan_repo_assets(root)}
    # Assert：声明投影 ScannedAsset.required_secrets；缺省空集
    assert assets["with-secrets"].required_secrets == ("GITHUB_TOKEN", "GH_TOKEN")
    assert assets["plain"].required_secrets == ()


def test_门1_聚合面非法凭证名_ValidationError():
    # Arrange & Act / Assert：扫描面宽容丢弃，聚合入参面 fail-closed（形状最后门）
    with pytest.raises(ValidationError, match="env 名形状"):
        SkillEntry(
            tenant_id=uuid.uuid4(),
            name="it-坏声明",
            source_uri="u://bad",
            version="1.0.0",
            required_secrets=("bad-name",),
        )
