"""仓库技能资产扫描器（docs/Agent/14 §6 S2「资产库扫描器=seed 数据源」）。

形状（以实文件为准采样的两种 frontmatter 变体，全仓 23 枚逐一核对均为单行标量）：

    变体 A（本仓手写狗粮）：``name:`` + ``description:``（无版本行）；
    变体 B（agentskills.io 先例，如 arxiv/pdf/spike）：``name`` + 带引号 ``description``
    + ``version`` + author/license/platforms/metadata 等扩展行。

解析口径：零新依赖（PyYAML 非声明依赖，不引）——``---`` 围栏块内逐行吃 ``key: value``
单行标量，值剥成对引号；metadata 等嵌套子行缩进不匹配顶层键模式自然跳过。缺 name 的
目录**跳过不中断**（扫描器是 seed 供给面，单资产脏不挡批）；version 缺省 "1.0.0"
（变体 A 无版本行）。body_bytes=SKILL.md 文件字节数（st_size）；source_uri=资产相对
路径（资产根起，POSIX 分隔符——14 §3「资产相对路径或外部 uri」的 repo 侧形态）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from services.platform.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_VERSION = "1.0.0"
_ASSET_FILE = "SKILL.md"
# 顶层键：行首即键名（无缩进）+ 冒号 + 单行标量；嵌套子行带缩进不匹配
_TOP_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$")


@dataclass(frozen=True)
class ScannedAsset:
    """单枚扫描产物（scan_repo_assets 行投影；upsert 判定键=name+version）。"""

    name: str
    description: str
    version: str
    source_uri: str
    body_bytes: int


def default_assets_root() -> Path:
    """缺省资产根=本模块定位的 services/skills（资产原位即数据源，14 §1）。"""
    return Path(__file__).resolve().parents[1]


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> dict[str, str]:
    """``---`` 围栏 frontmatter 的单行标量抽取（零依赖；形状见模块 docstring）。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":  # 围栏闭合
            break
        if line[:1].isspace():  # 嵌套子行（metadata: 之下）非顶层键
            continue
        match = _TOP_KEY_RE.match(line)
        if match is not None:
            fields[match.group(1)] = _strip_quotes(match.group(2))
    return fields


def scan_repo_assets(root: Path | str | None = None) -> list[ScannedAsset]:
    """遍历资产根下 ``<slug>/SKILL.md``（仅一跳子目录，根散文件不吃），产出扫描清单。

    root 缺省取 Settings.skills_repo_root（空=模块定位默认）；目录不存在抛 FileNotFoundError
    （配置错 fail-fast，不静默返空——列表为空与配置错是两回事）。
    """
    if root is None:
        configured = Settings().skills_repo_root
        root = Path(configured) if configured else default_assets_root()
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"skills 资产根不存在: {root}")

    assets: list[ScannedAsset] = []
    for skill_md in sorted(root.glob(f"*/{_ASSET_FILE}")):
        raw = skill_md.read_text(encoding="utf-8")
        fields = parse_frontmatter(raw)
        name = fields.get("name", "").strip()
        if not name:
            logger.warning("skills 扫描跳过无 name 资产: %s", skill_md)
            continue
        assets.append(
            ScannedAsset(
                name=name,
                description=fields.get("description", "").strip(),
                version=fields.get("version", "").strip() or DEFAULT_VERSION,
                # source_uri=资产根起相对路径（POSIX 分隔符，跨端稳定）
                source_uri=skill_md.resolve().relative_to(root.resolve()).as_posix(),
                body_bytes=skill_md.stat().st_size,
            )
        )
    return assets
