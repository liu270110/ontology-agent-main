# tests/skills/test_scanner.py
"""仓库技能资产扫描器单测（docs/Agent/14 §6 S2：对本仓 services/skills 真扫）。"""

from pathlib import Path

import pytest

from services.skills.business.scanner import (
    default_assets_root,
    parse_frontmatter,
    scan_repo_assets,
)

# 本仓狗粮实名锚点（services/skills/ 实目录采样；14 §1「14+ 资产」现勘认 23 枚）
_实名锚点 = ("frontend-dev-standards", "frontend-testing", "systematic-debugging", "wt-batch-close")


def test_真扫本仓资产_数量不少于10且含实名():
    # Act
    assets = scan_repo_assets()
    names = {a.name for a in assets}
    # Assert：≥10 条 + 实名命中（S2 验收行「列表含本仓 14+ 资产」）
    assert len(assets) >= 10
    for name in _实名锚点:
        assert name in names


def test_真扫资产_name与目录名一致():
    assets = scan_repo_assets()
    for asset in assets:
        assert asset.source_uri.startswith(f"{asset.name}/")  # <slug>/SKILL.md 相对路径形态
        assert asset.source_uri.endswith("SKILL.md")


def test_真扫资产_body_bytes均为正数():
    for asset in scan_repo_assets():
        assert asset.body_bytes > 0


def test_真扫幂等_两扫同形():
    # Act
    first, second = scan_repo_assets(), scan_repo_assets()
    # Assert：纯函数扫描两遍结果完全一致（入库幂等由 repo 层 uk 保证，见集成用例）
    assert first == second


def test_frontmatter_两种变体解析():
    # Arrange：变体 A（无引号无版本）与变体 B（agentskills.io 带引号带版本）
    plain = "---\nname: my-skill\ndescription: 无引号描述\n---\n\n# 正文\n"
    quoted = (
        '---\nname: quoted-skill\ndescription: "带引号描述."\nversion: 1.1.0\nauthor: x\nmetadata:\n  nested: y\n---\n'
    )
    # Act
    plain_fields = parse_frontmatter(plain)
    quoted_fields = parse_frontmatter(quoted)
    # Assert：引号剥离 + 嵌套子行不吃
    assert plain_fields["name"] == "my-skill"
    assert plain_fields["description"] == "无引号描述"
    assert quoted_fields["name"] == "quoted-skill"
    assert quoted_fields["description"] == "带引号描述."
    assert quoted_fields["version"] == "1.1.0"
    assert "nested" not in quoted_fields


def test_变体A缺版本行_默认1_0_0():
    # Arrange：真仓 code-review-ocr 为变体 A（无版本行）
    assets = {a.name: a for a in scan_repo_assets()}
    # Assert：缺省版本 "1.0.0"（scanner.DEFAULT_VERSION）
    assert assets["code-review-ocr"].version == "1.0.0"
    # 变体 B（arxiv 带 version: 1.0.0）照实读
    assert assets["arxiv"].version == "1.0.0"


def test_无frontmatter围栏返回空():
    assert parse_frontmatter("没有围栏的正文") == {}


def test_自定义根扫描(tmp_path: Path):
    # Arrange：最小资产目录（一枚有 name、一枚无 name）
    root = tmp_path / "assets"
    (root / "good").mkdir(parents=True)
    (root / "good" / "SKILL.md").write_text("---\nname: good\ndescription: d\n---\nbody", encoding="utf-8")
    (root / "bad").mkdir()
    (root / "bad" / "SKILL.md").write_text("---\ndescription: 无 name\n---\n", encoding="utf-8")
    (root / "散文件.md").write_text("根散文件不吃", encoding="utf-8")
    # Act
    assets = scan_repo_assets(root)
    # Assert：只收 good；无 name 跳过不中断；source_uri 相对根；body_bytes=文件字节数
    assert len(assets) == 1
    asset = assets[0]
    assert (asset.name, asset.description, asset.version, asset.source_uri) == ("good", "d", "1.0.0", "good/SKILL.md")
    assert asset.body_bytes == (root / "good" / "SKILL.md").stat().st_size


def test_根不存在抛错():
    with pytest.raises(FileNotFoundError, match="skills 资产根不存在"):
        scan_repo_assets(Path("Z:/不存在的路径/skills"))


def test_缺省根_模块定位等于仓库资产目录():
    # Assert：settings.skills_repo_root 空串时缺省根=模块上溯的 services/skills，真目录存在
    assert default_assets_root().is_dir()
    assert (default_assets_root() / "frontend-dev-standards" / "SKILL.md").is_file()
