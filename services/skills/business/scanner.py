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

required-secrets（K5 门 1 声明，方案依据=docs/Agent/13 §10；上游=deer-flow frontmatter
声明门缩减版）：单行逗号分隔标量同名键（``required-secrets: GITHUB_TOKEN, DEEPSEEK_API_KEY``），
parse_required_secrets 逐项剥空格、去重保序、非法 env 名丢弃并 warning（deer-flow parser
同款宽容口径：声明面坏项不挡批），空/缺省=空集；产出投影 ScannedAsset.required_secrets。

SkillScan findings（K30-a/b，方案依据=docs/Agent/13 §36）：每资产正文 + scripts/*.py 源码
过 skillscan 规则库（投毒指令/frontmatter 完备性/脚本危险模式+AST 精查），结论挂
ScannedAsset.findings 并 logger 留痕。**扫描器在此只留痕不拒收**——存量口径（13 §36 裁决：
存量命中 CRITICAL 仅 WARN 级留痕，ingest 种子路径不拒收）；拒收 teeth 在 register 新资产
（service.SkillScanRejectedError 4603）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from services.platform.config import Settings
from services.skills.business.skillscan import Finding, scan_skill_full
from services.skills.domain.model.skill import SECRET_NAME_RE

logger = logging.getLogger(__name__)

DEFAULT_VERSION = "1.0.0"
_ASSET_FILE = "SKILL.md"
_SCRIPTS_DIR = "scripts"
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
    required_secrets: tuple[str, ...] = ()  # K5 门 1：frontmatter 声明的凭证名（缺省空集）
    # K30-a：SkillScan 规则库结论（真实 severity；消费面裁决——ingest 留痕放行/register 拒收）
    findings: tuple[Finding, ...] = ()


def default_assets_root() -> Path:
    """缺省资产根=本模块定位的 services/skills（资产原位即数据源，14 §1）。"""
    return Path(__file__).resolve().parents[1]


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> dict[str, str]:
    """``---`` 围栏 frontmatter 的单行标量抽取（零依赖；形状见模块 docstring）。

    fail-open 口径（K5 P2① 明示，deer-flow parser 同款宽容边界）：重复键 last-wins
    （后行覆盖前行，不告警不挡批）；未知键静默接受原样透传（不做白名单校验，合法性
    归消费方裁断）——扫描器是 seed 供给面，单资产元数据脏不中断整批扫描（与模块
    docstring「缺 name 跳过不中断」同一边界，此处落到键级）。
    """
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


def parse_required_secrets(raw: str | None) -> tuple[str, ...]:
    """required-secrets 单行逗号分隔标量 → 凭证名元组（K5 门 1；宽容口径见模块 docstring）。

    空项跳过、逐项剥空白、去重保序；不匹配 env 名形状（SECRET_NAME_RE）的项丢弃并
    warning——声明面坏项不挡批（seed 供给面 scanner 同款），fail-closed 校验留给
    聚合入参面（SkillEntry field_validator）。
    """
    if not raw:
        return ()
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if SECRET_NAME_RE.fullmatch(name) is None:
            logger.warning("skills 声明 required-secrets 丢弃非法凭证名: %r", name)
            continue
        if name not in names:
            names.append(name)
    return tuple(names)


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
        findings, scripts = _scan_asset_scripts(skill_md, raw)
        assets.append(
            ScannedAsset(
                name=name,
                description=fields.get("description", "").strip(),
                version=fields.get("version", "").strip() or DEFAULT_VERSION,
                # source_uri=资产根起相对路径（POSIX 分隔符，跨端稳定）
                source_uri=skill_md.resolve().relative_to(root.resolve()).as_posix(),
                body_bytes=skill_md.stat().st_size,
                required_secrets=parse_required_secrets(fields.get("required-secrets")),
                findings=tuple(findings),
            )
        )
    return assets


def _scan_asset_scripts(skill_md: Path, md_text: str) -> tuple[list[Finding], dict[str, str]]:
    """读同目录 scripts/*.py 源码并跑 SkillScan 规则库（K30-a/b）；结论 logger 留痕。

    脚本读盘失败（IO/解码）按 SS-AST-001 CRITICAL 留痕——读不了的脚本与语法错脚本同属
    「内容不可验证」，fail-closed 口径。
    """
    scripts: dict[str, str] = {}
    extra: list[Finding] = []
    scripts_dir = skill_md.parent / _SCRIPTS_DIR
    if scripts_dir.is_dir():
        for py in sorted(scripts_dir.glob("*.py")):
            try:
                scripts[py.name] = py.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                extra.append(Finding("SS-AST-001", "CRITICAL", py.name, str(exc)[:80]))
    findings = [*scan_skill_full(md_text, scripts), *extra]
    if findings:
        n_crit = sum(1 for f in findings if f.severity == "CRITICAL")
        logger.warning(
            "skills 扫描资产 %s 命中 findings %d 条（CRITICAL %d / WARN %d）: %s",
            skill_md.parent.name,
            len(findings),
            n_crit,
            len(findings) - n_crit,
            "; ".join(str(f) for f in findings if f.severity == "CRITICAL") or "（仅 WARN）",
        )
    return findings, scripts
