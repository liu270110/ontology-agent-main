"""SKILL.md 技能目录装载与注入段渲染（能力通道竖线②，L1 纯提示层，2026-10-05 批）。

方案依据：docs/Skills §3.3（SKILL.md frontmatter：name/description 必填）+ §3.4 四通道
（L1 SKILL.md=程序性知识、纯提示层、无代码）+「渐进式加载」节（元数据层常驻：name/
one-line description 进系统提示技能目录；正文触发加载——本批只做第一级，正文不整篇
注入）+ docs/Agent/02 四通道权威；机制审计底稿 dwfrun-a1582cc6；本批设计（主会话
2026-10-05 定）。

**锚点快照（M3 Profile 落地前最小竖线）**：Profile/skills ref 未实装（侦察结论：Agent
config 白名单仅 model/temperature/tool_whitelist/num_ctx——domain/model/agent.py
_CONFIG_KEYS；skills ref 仅存在于 L2 能力包 server.json 设计稿 x-platform.skills[].ref）。
故装载来源锚点=config（Settings.skills_catalog_dir/include/exclude）而非 Profile；Profile
落地后本模块的「装载面」按 ref 收口，解析/渲染面可复用。

解析纪律（agentskills.io 兼容的受控子集）：frontmatter 须以 ``---`` 行开头、以 ``---`` 行
闭合；仅取顶层 ``name``/``description`` 单行标量（引号成对则剥离）；嵌套块（如 metadata:）
与折叠标量（``>/|`` 续行）不解析——name/description 落在这些形态的技能按缺字段降级跳过。
零第三方依赖（pyyaml 为传递依赖非直接依赖，仓库纪律「直接依赖必须显式声明」，受控子集
无需引入；复杂 YAML 形态需求出现时再立显式依赖收口）。坏文件跳过并 WARNING 留痕，
不影响其余技能装载（降级不阻塞会话启动）。

边界（本批不做）：市场分区/DB 存储/技能运行时执行——技能不产生可执行 tool，仅提示注入；
正文触发加载（渐进式第二级）与触发词判定随后续批次。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SKILL_MD_NAME = "SKILL.md"
_DESC_MAX_CHARS = 120  # 单条 description 截断上限（目录预算锚点=docs/Skills「渐进式加载」
# ≤0.5k tokens/全部技能；中英混排下 23 个捆绑技能约 1.5k chars 量级，截断保底不失控）


class SkillCatalogError(Exception):
    """SKILL.md frontmatter 不合格（缺失/无 frontmatter/name 或 description 缺失）。"""


@dataclass(frozen=True, slots=True)
class SkillCatalogEntry:
    """技能目录条目（元数据层）：仅 name+description 进系统提示，正文不装载。"""

    name: str
    description: str
    source: str  # SKILL.md 路径（留痕/审计用，不进提示）


def _clean_scalar(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1].strip()
    return v


def parse_skill_frontmatter(text: str) -> tuple[str, str]:
    """解析 SKILL.md frontmatter 的 name/description（受控子集；不合格抛 SkillCatalogError）。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillCatalogError("frontmatter 起始缺失（首行须为 ---）")
    fields: dict[str, str] = {}
    closed = False
    for raw in lines[1:]:
        if raw.strip() == "---":
            closed = True
            break
        if not raw.strip():
            continue
        if raw[0] in (" ", "\t"):
            continue  # 嵌套块/折叠标量续行：受控子集不解析
        key, sep, value = raw.partition(":")
        if not sep:
            continue  # 无冒号行：受控子集外形态，忽略
        if value.strip()[:1] in (">", "|"):
            continue  # 折叠/字面块标量指示行（>-/|-/|2 等变体）：受控子集不解析——不落字段，
            # 走「缺字段降级跳过+留痕」路径（否则 ">-"/"|" 会被当作 name/description 注入目录）
        fields[key.strip()] = value
    if not closed:
        raise SkillCatalogError("frontmatter 未闭合（缺收尾 --- 行）")
    name = _clean_scalar(fields.get("name", ""))
    description = _clean_scalar(fields.get("description", ""))
    if not name:
        raise SkillCatalogError("frontmatter 缺 name（agentskills.io 必填）")
    if not description:
        raise SkillCatalogError("frontmatter 缺 description（agentskills.io 必填）")
    return name, description


def load_skill_catalog(
    root: str | Path,
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
) -> tuple[SkillCatalogEntry, ...]:
    """扫描 ``{root}/*/{SKILL.md}`` 装载技能目录（确定性：按目录名排序；坏文件跳过留痕）。

    - root 不存在/非目录 → 空元组（降级不阻塞，WARNING 留痕）；
    - include 非空=白名单（按 name）；exclude 优先级高于 include；
    - 重名（不同目录同名技能）：排序序首个生效，其余 WARNING 留痕跳过。
    """
    root_path = Path(root)
    if not root_path.is_dir():
        logger.warning("skills catalog 根目录不可用，以空目录继续: %s", root_path)
        return ()
    include_set, exclude_set = set(include), set(exclude)
    entries: list[SkillCatalogEntry] = []
    seen: set[str] = set()
    for skill_dir in sorted(p for p in root_path.iterdir() if p.is_dir()):
        md = skill_dir / SKILL_MD_NAME
        if not md.is_file():
            continue
        try:
            name, description = parse_skill_frontmatter(md.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SkillCatalogError) as exc:
            logger.warning("skills catalog 跳过坏文件（降级不影响其余装载）: %s (%s)", md, exc)
            continue
        if include_set and name not in include_set:
            continue
        if name in exclude_set:
            continue
        if name in seen:
            logger.warning("skills catalog 重名技能跳过（首个生效）: %s (%s)", name, md)
            continue
        seen.add(name)
        entries.append(SkillCatalogEntry(name=name, description=description, source=md.as_posix()))
    return tuple(entries)


def render_skill_catalog_segment(entries: Iterable[SkillCatalogEntry]) -> str:
    """渲染目录注入段（确定性纯函数：按 name 排序 + 固定序列化，同 render_tool_schema_section
    的 KV-cache 前缀稳定纪律）；空集返回空串（不注入裸表头）。"""
    lines: list[str] = []
    for entry in sorted(entries, key=lambda e: e.name):
        description = entry.description
        if len(description) > _DESC_MAX_CHARS:
            description = description[:_DESC_MAX_CHARS] + "…"
        lines.append(f"- {entry.name}: {description}")
    if not lines:
        return ""
    return "\n".join(["【技能目录（元数据层：name+description；正文不随提示注入，按需加载）】", *lines])
