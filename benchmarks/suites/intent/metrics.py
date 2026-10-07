"""intent 四指标——口径唯一事实源（docs/Agent/16 §2 intent 行冻结；本文件即口径字典实现）。

四维（同金标集，A0/A1 双档同口径可比）：
1. intent_accuracy：期望映射条（expectation=map，clear 40+ambiguous 30）中 action 与
   金标一致的比例；整体 + 按 ambiguity_level 分层（clear/ambiguous 各一列）。
2. clarification_trigger_rate：期望澄清条（need_clarification 20）中选 ask_user 的比例
   ——「信息不足应触发澄清而非猜」（红队 D 域/E2 判据）。
3. out_of_scope_reject_rate：越界条（out_of_scope 10）中被判拒的比例；判拒口径=
   action="out_of_scope"（显式拒）**或** confidence < out_of_scope_confidence_floor
   （「映射最近+低置信」亦算诚实拒绝——ask 定义的两条合法出路）。
4. ontology_constraint_gain：A1（本体约束档）− A0（无约束直觉档）于上述三项的差值列
   （E2 核心假设 A/B——负值同样是攻击性提问的真实答案）。

解析口径：模型输出须为 JSON {"action","confidence"}；剥 <think> 块后取首个平衡 JSON
对象；解析失败记 parse_error（计入分母、各率均按失败计——输出契约违规是行为的一部分，
不静默重试）。

全部函数为纯函数（时间与模型输出经参数注入），独立可单测。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# ---- 口径常量（冻结进本文件；改动=口径版本变化，须在 README 口径字典同步登记）----
CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 1.0
OUT_OF_SCOPE_ACTION = "out_of_scope"  # 越界判拒的显式动作词（输出契约保留词）
CLARIFY_ACTION = "ask_user"  # 平台澄清行动类（候选集内合法动作；need_clarification 金标期望）


@dataclass(slots=True, frozen=True)
class ParsedAnswer:
    """模型输出解析产物（confidence 缺失记 None——仅影响越界判拒的低置信通路）。"""

    action: str
    confidence: float | None


@dataclass(slots=True)
class ItemVerdict:
    """单条金标在单档下的判定记录（结果 JSON per_item 数组的最小单元）。"""

    item_id: str
    ambiguity_level: str
    expectation: str  # map | clarify | reject
    expected_action: str | None
    predicted_action: str | None  # None=parse_error
    confidence: float | None
    parse_error: bool
    correct: bool = False  # map 条命中
    clarified: bool = False  # clarify 条触发
    rejected: bool = False  # reject 条判拒
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw_output: str = field(default="", repr=False)


# ---------------------------------------------------------------- 输出解析（纯函数）


def _strip_think(text: str) -> str:
    """剥 qwen3 内联思考块（<think>...</think>；未闭合 think 视为后续全文皆思考）。"""
    if "<think>" not in text:
        return text
    head, _, rest = text.partition("<think>")
    _, end_tag, tail = rest.partition("</think>")
    if end_tag:
        return head + " " + tail
    return head  # 未闭合：思考未结束（截断），正文视作空


def _first_balanced_json(text: str) -> str | None:
    """取首个平衡 JSON 对象子串（扫描花括号配对，忽略字符串字面量内括号）。"""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : idx + 1]
        start = text.find("{", start + 1)
    return None


def parse_prediction(raw: str) -> ParsedAnswer | None:
    """原始输出 → ParsedAnswer；无合法 JSON/缺 action/非字符串 action → None（parse_error）。"""
    blob = _first_balanced_json(_strip_think(raw))
    if blob is None:
        return None
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    action = obj.get("action")
    if not isinstance(action, str) or not action.strip():
        return None
    confidence = obj.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = None
    else:
        confidence = max(CONFIDENCE_MIN, min(CONFIDENCE_MAX, float(confidence)))
    return ParsedAnswer(action=action.strip(), confidence=confidence)


# ---------------------------------------------------------------- 单条判分（纯函数）


def score_item(
    *,
    item_id: str,
    ambiguity_level: str,
    expectation: str,
    expected_action: str | None,
    raw_output: str,
    confidence_floor: float,
    latency_ms: float = 0.0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> ItemVerdict:
    """单条判定：parse_error 全率记败；map/clarify/reject 三期望各按口径 2/3/4 判定。"""
    parsed = parse_prediction(raw_output)
    verdict = ItemVerdict(
        item_id=item_id,
        ambiguity_level=ambiguity_level,
        expectation=expectation,
        expected_action=expected_action,
        predicted_action=None if parsed is None else parsed.action,
        confidence=None if parsed is None else parsed.confidence,
        parse_error=parsed is None,
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        raw_output=raw_output,
    )
    if parsed is None:
        return verdict
    if expectation == "map":
        verdict.correct = parsed.action == expected_action
    elif expectation == "clarify":
        verdict.clarified = parsed.action == CLARIFY_ACTION
    elif expectation == "reject":
        low_confidence = parsed.confidence is not None and parsed.confidence < confidence_floor
        verdict.rejected = parsed.action == OUT_OF_SCOPE_ACTION or low_confidence
    return verdict


# ---------------------------------------------------------------- 聚合（纯函数）


@dataclass(slots=True)
class IntentMetrics:
    """单档四维聚合（口径见模块头；全比率分母含 parse_error——契约违规是行为）。"""

    intent_accuracy: float  # map 条整体
    intent_accuracy_clear: float  # map 条 ∩ clear
    intent_accuracy_ambiguous: float  # map 条 ∩ ambiguous
    clarification_trigger_rate: float  # clarify 条
    out_of_scope_reject_rate: float  # reject 条
    parse_error_count: int
    map_count: int
    clarify_count: int
    reject_count: int
    latency_p50_ms: float
    cost_per_item_tokens: float
    per_item: list[ItemVerdict] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_accuracy": self.intent_accuracy,
            "intent_accuracy_by_level": {
                "clear": self.intent_accuracy_clear,
                "ambiguous": self.intent_accuracy_ambiguous,
            },
            "clarification_trigger_rate": self.clarification_trigger_rate,
            "out_of_scope_reject_rate": self.out_of_scope_reject_rate,
            "parse_error_count": self.parse_error_count,
            "counts": {"map": self.map_count, "clarify": self.clarify_count, "reject": self.reject_count},
            "latency_p50_ms": self.latency_p50_ms,
            "cost_per_item_tokens": self.cost_per_item_tokens,
        }


def _ratio(hits: int, total: int) -> float:
    return hits / total if total else 0.0


def aggregate(verdicts: Sequence[ItemVerdict], *, latency_p50_ms: float = 0.0) -> IntentMetrics:
    """宏平均聚合（纯函数）：三期望条各按各自分母聚合，parse_error 留在分母内。"""
    map_rows = [v for v in verdicts if v.expectation == "map"]
    clear_rows = [v for v in map_rows if v.ambiguity_level == "clear"]
    ambiguous_rows = [v for v in map_rows if v.ambiguity_level == "ambiguous"]
    clarify_rows = [v for v in verdicts if v.expectation == "clarify"]
    reject_rows = [v for v in verdicts if v.expectation == "reject"]
    total_tokens = sum(v.prompt_tokens + v.completion_tokens for v in verdicts)
    return IntentMetrics(
        intent_accuracy=_ratio(sum(1 for v in map_rows if v.correct), len(map_rows)),
        intent_accuracy_clear=_ratio(sum(1 for v in clear_rows if v.correct), len(clear_rows)),
        intent_accuracy_ambiguous=_ratio(sum(1 for v in ambiguous_rows if v.correct), len(ambiguous_rows)),
        clarification_trigger_rate=_ratio(sum(1 for v in clarify_rows if v.clarified), len(clarify_rows)),
        out_of_scope_reject_rate=_ratio(sum(1 for v in reject_rows if v.rejected), len(reject_rows)),
        parse_error_count=sum(1 for v in verdicts if v.parse_error),
        map_count=len(map_rows),
        clarify_count=len(clarify_rows),
        reject_count=len(reject_rows),
        latency_p50_ms=latency_p50_ms,
        cost_per_item_tokens=_ratio(total_tokens, len(verdicts)),
        per_item=list(verdicts),
    )


def ontology_constraint_gain(metrics_a0: IntentMetrics, metrics_a1: IntentMetrics) -> dict[str, Any]:
    """E2 核心产出：A1（本体约束档）− A0（直觉档）四指标差值列（纯函数）。

    负值如实保留——增益为负同样是对「本体约束是否有效」这一攻击性提问的回答。
    """
    return {
        "definition": "A1（本体约束/语义标注增强提示）− A0（行动类名清单直觉）于四指标差值",
        "diff": {
            "intent_accuracy": metrics_a1.intent_accuracy - metrics_a0.intent_accuracy,
            "intent_accuracy_clear": metrics_a1.intent_accuracy_clear - metrics_a0.intent_accuracy_clear,
            "intent_accuracy_ambiguous": metrics_a1.intent_accuracy_ambiguous - metrics_a0.intent_accuracy_ambiguous,
            "clarification_trigger_rate": metrics_a1.clarification_trigger_rate - metrics_a0.clarification_trigger_rate,
            "out_of_scope_reject_rate": metrics_a1.out_of_scope_reject_rate - metrics_a0.out_of_scope_reject_rate,
        },
        "a0": metrics_a0.to_dict(),
        "a1": metrics_a1.to_dict(),
    }
