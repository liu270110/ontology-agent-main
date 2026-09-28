"""ORSI 进化面注册表（architecture/09 §13.2 进化面矩阵 + §13.5 rsi:EvolutionSurface）。

八大进化面是**封闭注册表**（§13.2 分界铁律逐字）：进化面注册表本身是平台内置本体模块
（随任务本体交付），**新增进化面 = 本体变更 = 人工审批**——"改进化机制"本身也被本体控制
（递归有界的本体化表达）。因此本注册表：只读、内置八面、运行期任何路径不得动态扩面
（``surface_of`` 对未局面名直接抛错；扩面只能改本文件并随代码评审/本体变更审批通过）。

面间分界铁律（§13.2，G0 开单选面的依据）：**O1 只新增/替换行动类的实现绑定，不得触碰
行动类语义**——语义要变即 O2，走 changeset 人工终审；同理 O5 固化的是"已验证可形式化"的
判断，SHACL 基线与公理永远不在任何进化面内（红线 §6.2）。

元数据四元组逐字对齐 §13.2 八行表：本体锚点（改动落点）/ 触发信号 / 进化产物（挂靠）/
效果判据。本模块零外部依赖（值对象层，供 gap.py 缺口工单与后续 rsi:Candidate 本体化引用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EvolutionSurface(StrEnum):
    """八大进化面（§13.2 定稿；枚举值 = 面编号，封闭集合，扩面=代码变更=人工审批）。"""

    TOOL_IMPL = "O1"  # 工具实现
    ACTION_SEMANTICS = "O2"  # 行动类语义
    PROMPT = "O3"  # 提示词
    SKILL = "O4"  # 技能
    CONTROL_FLOW = "O5"  # 控制流程
    MEMORY_POLICY = "O6"  # 记忆策略
    RETRIEVAL_POLICY = "O7"  # 检索策略
    MODEL_WEIGHT = "O8"  # 模型权重


@dataclass(frozen=True, slots=True)
class SurfaceMeta:
    """单面元数据（§13.2 矩阵行的四元组，逐字承载）。"""

    name: str  # 面名（§13.2 行名）
    ontology_anchor: str  # 本体锚点（改动落点在本体中的位置）
    trigger_signals: str  # 触发信号（什么时候需要改）
    artifact: str  # 进化产物（改出来的东西挂靠哪）
    criterion: str  # 效果判据（怎么算改好了）


# 内置八面注册表（§13.2 八行表逐字对齐；封闭集合——新增面 = 本体变更 = 人工审批，
# 不得在运行期/代码外动态注册，「改进化机制」本身也被本体控制，§13.2 分界铁律）。
REGISTRY: dict[EvolutionSurface, SurfaceMeta] = {
    EvolutionSurface.TOOL_IMPL: SurfaceMeta(
        name="工具实现",
        ontology_anchor="行动类 ob2:Action 的实现绑定（工具=行动类的实现，ontology §2.2 铁律）",
        trigger_signals="行动类缺绑定 / 工具失败率↑时延↑ / 人工接管聚类",
        artifact="用户/租户工具集（能力包，带行动类语义标注）",
        criterion="同行动类任务成功率↑、步数/时延↓",
    ),
    EvolutionSurface.ACTION_SEMANTICS: SurfaceMeta(
        name="行动类语义",
        ontology_anchor="本体元素 ob2:Action 本身",
        trigger_signals="任务无法归类（unmapped intent 聚类）、CQ 覆盖缺口",
        artifact="候选行动类 → changeset（人工终审）",
        criterion="CQ 覆盖率、术语对齐通过",
    ),
    EvolutionSurface.PROMPT: SurfaceMeta(
        name="提示词",
        ontology_anchor="策略/模板类（四类抽取模板等）",
        trigger_signals="归因：同型失败反复、输出格式违例聚类",
        artifact="模板 patch → 提示词版本库",
        criterion="抽取精确率 / 任务成功率回归",
    ),
    EvolutionSurface.SKILL: SurfaceMeta(
        name="技能",
        ontology_anchor="SKILL.md 程序记忆（hermes 式）",
        trigger_signals="成功轨迹聚类（可复用模式）",
        artifact="候选技能 → Skills 库候选区",
        criterion="同型任务步数↓、零工具调用率↑",
    ),
    EvolutionSurface.CONTROL_FLOW: SurfaceMeta(
        name="控制流程",
        ontology_anchor="task:Plan 模板档 + R3 规则",
        trigger_signals="高频确定链路仍走 LLM（推理分级漏判）",
        artifact="计划模板固化 / SPARQL CONSTRUCT 规则 → changeset",
        criterion="确定性路由率↑（零 token 化）、门禁拦截率",
    ),
    EvolutionSurface.MEMORY_POLICY: SurfaceMeta(
        name="记忆策略",
        ontology_anchor="mem 参数（召回权重/半衰期/升级阈值）",
        trigger_signals="召回质量基准退化",
        artifact="参数版本",
        criterion="golden 召回集",
    ),
    EvolutionSurface.RETRIEVAL_POLICY: SurfaceMeta(
        name="检索策略",
        ontology_anchor="检索模式配比/top-k/融合权重",
        trigger_signals="检索基准退化",
        artifact="参数版本（强制回归，08 §7.2）",
        criterion="golden QA 分层集",
    ),
    EvolutionSurface.MODEL_WEIGHT: SurfaceMeta(
        name="模型权重",
        ontology_anchor="适配器资产（pinning，§12.7）",
        trigger_signals="程序性收益递减（§9 v2 触发条件）",
        artifact="QLoRA→GGUF→渠道切流（ops/03）",
        criterion="holdout 盲测集",
    ),
}

assert set(REGISTRY) == set(EvolutionSurface), "进化面注册表必须与枚举一一对应（封闭八面，§13.2）"


def surface_of(name: str) -> SurfaceMeta:
    """按面名查询元数据（"O1"/"o1" 均可）。

    未局面名抛 ``KeyError``——封闭注册表不存在动态面（扩面 = 代码变更 = 人工审批，§13.2）。
    """
    try:
        surface = EvolutionSurface(name.strip().upper())
    except ValueError as exc:
        raise KeyError(f"未局面名: {name!r}（封闭八面注册表，§13.2：扩面=代码变更=人工审批）") from exc
    return REGISTRY[surface]
