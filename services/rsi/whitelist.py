"""RSI 五类改进白名单（architecture/09 §3：递归有界的机械执行点，不依赖模型自觉）。

清单校验铁律（09 §3，红线 §6 的机械形态）：``type ∉ 五类``，或 ``target`` 指向**内核代码/
扩展点契约/本体公理与 SHACL 基线/权限/审批/沙箱策略/评估器** → 直接拒绝并记安全审计。

不含内核代码与公理（docs/Agent/02「内核不可下放」）：内核 B 类安全与信任基线硬编码于内核，
RSI 零改动；公理/TBox 属本体版本治理，RSI 只能经记忆 L3→L4 反哺通道提名（09 §12.2）。
载体清单按现行体系（2026-09-26 裁决）：候选技能/规则/本体概念三类已移出白名单，分别走
Skills 库上架通道 / 本体 ChangeRequest / 反哺通道——防「改规则让自己通过」。
"""

from __future__ import annotations

from enum import StrEnum


class ImprovementType(StrEnum):
    """五类改进白名单（09 §3 表；载体与生效位置见该表，此处只承载枚举语义）。"""

    PROMPT_TEMPLATE = "prompt_template"  # ① 提示词模板（提示词版本库 patch）
    PLAN_TEMPLATE = "plan_template"  # ② 计划模板（规划模板档，Plan 仍本体实例过三层校验）
    MEMORY_POLICY = "memory_policy"  # ③ 记忆策略参数（召回权重/半衰期/升级阈值版本）
    RETRIEVAL_PARAMS = "retrieval_params"  # ④ 检索参数（模式配比/top-k/融合权重版本，强制回归）
    TOOL_DESCRIPTION = "tool_description"  # ⑤ 工具描述（描述文本与语义标注，不改行为实现与授权）


# 目标禁区标记（§3 铁律的机械执行：target 命中任一标记即拒绝+安全审计；大小写不敏感）。
# 评估器/评测集隔离（红线 3）：评估集与评估逻辑不在 RSI 可写范围。
FORBIDDEN_TARGET_MARKERS: tuple[str, ...] = (
    "services.",  # 平台代码（含内核模块树）
    "services/",
    "kernel",  # 内核代码与扩展点契约（Agent/02 §2.2 B 类零改动）
    "axiom",  # 本体公理
    "shacl",  # SHACL 基线（门禁基线不可降，红线 4）
    "ontology:",  # 本体治理对象（走 changeset，不进 RSI 管线）
    "permission",  # 权限
    "scope",  # 授权面
    "approval",  # 审批路由
    "sandbox",  # 沙箱策略
    "evaluator",  # 评估器（评估器隔离，红线 3）
    "eval_set",  # 评测集（含 holdout/回归集）
    "golden",  # 金标集
)


class WhitelistViolation(Exception):
    """白名单校验失败（§3 铁律；调用方必须落安全审计——ACTION_WHITELIST_VIOLATION）。"""


def validate_improvement(type_str: str, target: str) -> ImprovementType:
    """清单校验（0 级门禁的清单半边）：返回合法 ImprovementType；违规抛 WhitelistViolation。"""
    try:
        improvement_type = ImprovementType(type_str)
    except ValueError as exc:
        raise WhitelistViolation(f"改进类型不在五类白名单: {type_str}（09 §3）") from exc
    lowered = (target or "").lower()
    hit = next((marker for marker in FORBIDDEN_TARGET_MARKERS if marker in lowered), None)
    if hit is not None:
        raise WhitelistViolation(f"改进目标指向禁区（命中标记 {hit!r}）: {target}（09 §3 铁律/§6 红线）")
    if not target or not target.strip():
        raise WhitelistViolation("改进目标为空（须为载体标识 + 基线版本号）")
    return improvement_type
