#!/usr/bin/env python3
"""PoC5 LLM 结构化输出兼容矩阵 harness（实施计划 13 篇 §3.4；契约先例 services/kb/business/kb_extraction.py）。

矩阵：模型渠道 × 输出模式 × schema × 样例。
- 渠道：主=本地 vLLM local-main（qwen3-4b-awq，127.0.0.1:18001）；fallback=DeepSeek 云渠道
  （.env 有 key 即测；key 只在注释行时按「注释态 fallback」口径启用，可用 --no-cloud 跳过）。
  max_tokens 按渠道取值：本地 900 / 云 4000（云推理型模型思考耗 token 上限，防截断伪失败）。
- 输出模式：json_object（response_format）/ json_schema（response_format.structured）/ prompt（纯提示词+稳健解析）。
  每渠道先做模式预检（小调用探 response_format 支持度），不支持的模式整格跳过并记
  mode_unsupported——该事实本身=兼容矩阵数据点。
- schema：S1=_EXTRACT_SCHEMA_V1（逐字段复制 services/kb/business/kb_extraction.py 抽取输出 schema，
  提示词带本体谓词/类型白名单，对齐生产 extract 步的本体引导与 poc2 词汇表）；
  S2=自造嵌套 schema（故障工单结构化：嵌套对象×2 + 对象数组×2 + enum/数值约束）。
- 样例：每 schema 20 条电力域（S1=复用 poc2 语料 12 篇 + 增补 8 篇；S2=模板确定性生成 20 条，
  全字段自造真值）。

指标（对自造真值）：三态判定=first_pass（首次解析+校验双过）/ retry_pass（重试一次后过）/
fail（重试后仍不过或调用失败）；schema 通过率（首次/重试后）/ 字段级 F1（解析成功样例）/
延迟（仅首次尝试口径，本地格与云端格不可直接互比）。冻结产出：fallback 模型名单 + 推荐输出
模式组合 → 报告 docs/architecture/PoC⑤-结构化输出兼容矩阵报告.md；明细落 services/devtools/poc5_results.json。

用法：
  python services/devtools/poc5_structured_output.py                # 全矩阵（本地 + 云 fallback）
  python services/devtools/poc5_structured_output.py --no-cloud     # 只跑本地模型
  python services/devtools/poc5_structured_output.py --limit 2      # 每格只跑前 2 条样例（自检）
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any, Final

import httpx
from jsonschema import Draft202012Validator

REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUTPUT: Final = REPO_ROOT / "services" / "devtools" / "poc5_results.json"
HTTP_TIMEOUT_S: Final = 150.0
PROBE_TIMEOUT_S: Final = 6.0
MAX_TOKENS_LOCAL: Final = 900  # 本地 vLLM 无思考损耗，短输出足够
MAX_TOKENS_CLOUD: Final = 4000  # 云推理型模型思考消耗 token 上限，防截断伪失败（渠道适配配置）
N_SAMPLES_S2: Final = 20
MODES: Final[tuple[str, ...]] = ("json_object", "json_schema", "prompt")

# ---------------------------------------------------------------------------
# S1：_EXTRACT_SCHEMA_V1（逐字段复制 services/kb/business/kb_extraction.py，注明出处）
# ---------------------------------------------------------------------------

_EXTRACT_SCHEMA_V1: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidates"],
    "properties": {
        "candidates": {
            "type": "array",
            "maxItems": 64,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "name", "confidence"],
                "properties": {
                    "kind": {"enum": ["entity", "relation", "attribute", "event"]},
                    "name": {"type": "string", "minLength": 1, "maxLength": 256},
                    "ontology_class": {"type": "string", "maxLength": 256},
                    "predicate": {"type": "string", "maxLength": 256},
                    "object": {"type": "string", "maxLength": 1024},
                    "object_class": {"type": "string", "maxLength": 256},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "detail": {"type": "string", "maxLength": 2048},
                    "properties": {"type": "object", "additionalProperties": {"type": "string"}},
                },
            },
        }
    },
}

# ---------------------------------------------------------------------------
# S2：自造嵌套 schema（故障工单结构化；嵌套对象/对象数组/enum/数值约束）
# ---------------------------------------------------------------------------

_TICKET_SCHEMA_V1: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ticket", "fault", "crews", "actions", "confidence"],
    "properties": {
        "ticket": {
            "type": "object",
            "additionalProperties": False,
            "required": ["no", "priority"],
            "properties": {"no": {"type": "string"}, "priority": {"enum": ["高", "中", "低"]}},
        },
        "fault": {
            "type": "object",
            "additionalProperties": False,
            "required": ["feeder", "section", "status"],
            "properties": {
                "feeder": {"type": "string"},
                "section": {"type": "string"},
                "status": {"enum": ["故障停运", "已恢复"]},
            },
        },
        "crews": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "lead"],
                "properties": {"name": {"type": "string"}, "lead": {"type": "string"}},
            },
        },
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "target"],
                "properties": {"type": {"enum": ["隔离", "抢修", "送电", "巡线"]}, "target": {"type": "string"}},
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

SCHEMAS: Final[dict[str, dict[str, Any]]] = {"S1_extract_v1": _EXTRACT_SCHEMA_V1, "S2_ticket_v1": _TICKET_SCHEMA_V1}

# ---------------------------------------------------------------------------
# 样例语料（全部自造真值）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractSample:
    """S1 样例：文本 + 金标候选（kind/name/ontology_class/predicate/object；confidence 不进 F1）。"""

    doc_id: str
    text: str
    gold: tuple[dict[str, str], ...]


def _rel(sub: str, cls: str, pred: str, obj: str, obj_cls: str = "") -> dict[str, str]:
    """金标关系候选构造简写。"""
    return {
        "kind": "relation",
        "name": sub,
        "ontology_class": cls,
        "predicate": pred,
        "object": obj,
        "object_class": obj_cls,
    }


def _ent(name: str, cls: str) -> dict[str, str]:
    """金标实体候选构造简写（谓词留空；类型陈述统一映射为实体候选）。"""
    return {"kind": "entity", "name": name, "ontology_class": cls, "predicate": "", "object": "", "object_class": ""}


# 增补 8 篇（doc_id 以 poc5-* 标记，与 poc2 语料区分；金标口径与 poc2 一致）
EXTRA_EXTRACT_SAMPLES: Final[tuple[ExtractSample, ...]] = (
    ExtractSample(
        "poc5-ex-01",
        "调度日志：10kV望江线221开关过流保护动作跳闸，望江线全线失压，望江线北段用户停电。",
        (_rel("望江线221开关", "开关", "inSection", "望江线北段"), _rel("望江线", "馈线", "hasStatus", "故障停运")),
    ),
    ExtractSample(
        "poc5-ex-02",
        "故障工单GD-2026-0401已生成，抢修四班被派往望江线北段处理故障，负责人赵德芳。",
        (
            _rel("工单GD-2026-0401", "工单", "dispatchedTo", "抢修四班"),
            _rel("抢修四班", "抢修队", "crewLead", "赵德芳"),
        ),
    ),
    ExtractSample(
        "poc5-ex-03",
        "设备台账：南湖3号配变容量800kVA，接于望江线北段，向第二小学供电。",
        (
            _rel("南湖3号配变", "配变", "inSection", "望江线北段"),
            _rel("第二小学", "用户", "inSection", "望江线北段"),
        ),
    ),
    ExtractSample(
        "poc5-ex-04",
        "操作票P-2026-0101：望江线221开关由运行转检修，操作班组抢修四班，11时00分操作完成。",
        (_rel("望江线221开关", "开关", "hasStatus", "检修"), _rel("望江线北段", "区段", "confirmedAt", "11时00分")),
    ),
    ExtractSample(
        "poc5-ex-05",
        "月度分析：4月望江线共跳闸3次，均位于望江线北段；农贸市场由望江线北段供电。",
        (_rel("农贸市场", "用户", "inSection", "望江线北段"), _ent("望江线北段", "区段")),
    ),
    ExtractSample(
        "poc5-ex-06",
        "调度日志：西城线过负荷告警解除，西城线恢复正常；纺织厂负荷已切回西城线供电。",
        (_rel("西城线", "馈线", "hasStatus", "正常"), _rel("纺织厂", "用户", "inSection", "西城线")),
    ),
    ExtractSample(
        "poc5-ex-07",
        "故障报告：4月2日16时10分东环线978开关跳闸，抢修二班现场负责人张建国确认绝缘子击穿。",
        (
            _rel("东环线978开关", "开关", "inSection", "东环线工业区段"),
            _rel("抢修二班", "抢修队", "crewLead", "张建国"),
        ),
    ),
    ExtractSample(
        "poc5-ex-08",
        "抢修完成：朝阳线北段12时30分恢复送电，故障工单GD-2026-0401归档，归档时间18时00分。",
        (
            _rel("朝阳线北段", "区段", "restoredAt", "12时30分"),
            _rel("工单GD-2026-0401", "工单", "restoredAt", "18时00分"),
        ),
    ),
)


def load_s1_samples() -> list[ExtractSample]:
    """S1 样例 = 复用 poc2 语料 12 篇（金标三元组映射为候选；rdf:type→实体惯例）+ 增补 8 篇 = 20 篇。"""
    samples: list[ExtractSample] = []
    try:
        sys.path.insert(0, str(REPO_ROOT / "services" / "devtools"))
        import poc2_confidence_calibration as poc2
    except ImportError:
        print("[警告] poc2 语料导入失败，S1 仅用增补 8 篇（样例数不足 20，结果需标注）", flush=True)
        return list(EXTRA_EXTRACT_SAMPLES)
    for doc in poc2.SAMPLE_DOCS:
        gold: list[dict[str, str]] = []
        for t in doc.gold:
            pred = t.predicate.split("#")[-1].split(":")[-1]
            if pred == "type":
                gold.append(_ent(t.subject, t.object))
            else:
                gold.append(_rel(t.subject, t.subject_type, pred, t.object, t.object_type))
        samples.append(ExtractSample(doc.doc_id, doc.text, tuple(gold)))
    return samples + list(EXTRA_EXTRACT_SAMPLES)


# -- S2：模板确定性生成 20 条故障工单样例（文本与金标同源生成） -----------------------

_FEEDERS: Final[tuple[str, ...]] = ("滨河线", "朝阳线", "东环线", "西城线", "南郊线", "开发区线", "望江线", "龙潭线")
_SECTIONS: Final[tuple[str, ...]] = ("东段", "北段", "工业区段", "南段")
_CREWS: Final[tuple[tuple[str, str], ...]] = (
    ("抢修一班", "李卫东"),
    ("抢修二班", "张建国"),
    ("抢修三班", "王志强"),
    ("抢修四班", "赵德芳"),
)
_PRIORITIES: Final[tuple[str, ...]] = ("高", "中", "低")
_TARGETS: Final[tuple[str, ...]] = ("故障点杆塔", "主线开关", "分支开关", "配变高压侧")


@dataclass(frozen=True, slots=True)
class TicketSample:
    """S2 样例：工单文本 + 金标结构化对象（与 schema 同构，字段级真值完整）。"""

    doc_id: str
    text: str
    gold: dict[str, Any]


def build_s2_samples(n: int = N_SAMPLES_S2) -> list[TicketSample]:
    """参数网格确定性展开前 n 个组合，文本与金标同源生成。"""
    combos = list(product(range(len(_FEEDERS)), range(len(_SECTIONS)), range(len(_CREWS)), range(len(_PRIORITIES))))
    samples: list[TicketSample] = []
    for idx, (fi, si, ci, pi) in enumerate(combos[:n]):
        feeder, section = _FEEDERS[fi], _SECTIONS[si]
        crew, lead = _CREWS[ci]
        priority = _PRIORITIES[pi]
        no = f"GD-2026-05{idx:02d}"
        switch = f"{feeder}{idx + 10:03d}开关"
        target = _TARGETS[idx % len(_TARGETS)]
        gold: dict[str, Any] = {
            "ticket": {"no": no, "priority": priority},
            "fault": {"feeder": feeder, "section": f"{feeder}{section}", "status": "故障停运"},
            "crews": [{"name": crew, "lead": lead}],
            "actions": [
                {"type": "隔离", "target": switch},
                {"type": "抢修", "target": target},
                {"type": "送电", "target": f"{feeder}{section}"},
            ],
            "confidence": 0.9,
        }
        text = (
            f"故障工单{no}（优先级{priority}）：10kV{feeder}{section}故障停运，"
            f"{switch}保护动作。调度已通知{crew}（负责人{lead}）赶赴现场，"
            f"处置安排：先对{switch}执行隔离，随后对{target}组织抢修，具备条件后对{feeder}{section}恢复送电。"
        )
        samples.append(TicketSample(f"poc5-tk-{idx:02d}", text, gold))
    return samples


# ---------------------------------------------------------------------------
# 解析 / 校验 / 匹配工具
# ---------------------------------------------------------------------------

_THINK: Final = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE: Final = re.compile(r"```(?:json)?\s*|\s*```")
_WS: Final = re.compile(r"\s+")
_CJK: Final = re.compile(r"[\u4e00-\u9fff]")


def strip_decorations(content: str) -> str:
    """剥 think 块与代码围栏。"""
    return _FENCE.sub("", _THINK.sub("", content)).strip()


def robust_parse(content: str) -> tuple[object, str]:
    """稳健 JSON 解析（剥围栏后定位首尾括号）——prompt 模式与兜底共用。"""
    cleaned = strip_decorations(content)
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = cleaned.find(open_ch)
        end = cleaned.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1]), ""
            except json.JSONDecodeError:
                continue
    return [], "json_parse_failed"


def parse_by_mode(mode: str, content: str) -> tuple[object | None, str]:
    """按模式解析：prompt=稳健解析；json_object/json_schema=严格 loads（失败回落稳健解析）。

    返回 (parsed|None, err)。err ∈ ""（成功）/ json_parse_recovered（严格失败但兜底救回）/
    json_null（模型输出字面 null）/ json_parse_failed。
    """
    if mode == "prompt":
        data, err = robust_parse(content)
        return (data if data else None), err
    try:
        data = json.loads(strip_decorations(content))
    except json.JSONDecodeError:
        data, _ = robust_parse(content)
        if data:
            return data, "json_parse_recovered"  # 引擎保证失效但可救——单独计数口径
        return None, "json_parse_failed"
    if data is None:
        return None, "json_null"  # 字面 null=模型拒答形态，单列（实测 deepseek-flash 抽取任务出现）
    return data, ""


def validate_schema(data: object, schema: dict[str, Any]) -> list[str]:
    """jsonschema 校验，返回错误消息列表（空=通过）。"""
    validator = Draft202012Validator(schema)
    return [f"{list(err.absolute_path)}: {err.message}" for err in sorted(validator.iter_errors(data), key=str)]


def norm_value(text: str) -> str:
    """值归一：NFKC + 去空白 + 小写（字段级比对确定性口径）。"""
    return _WS.sub("", unicodedata.normalize("NFKC", text)).lower()


def value_match(pred_val: str, gold_val: str) -> bool:
    """字段值比对：归一全等；含中文的长值允许互相包含（模型对 target/section 的上下文补全可回收）。

    纯 ASCII/数值（工单号、枚举）必须全等——"0.9"⊂"0.95" 之类不适用软匹配。
    """
    na, nb = norm_value(pred_val), norm_value(gold_val)
    if na == nb:
        return True
    if not na or not nb:
        return False
    if _CJK.search(na) or _CJK.search(nb):
        return na in nb or nb in na
    return False


def bigrams(text: str) -> frozenset[str]:
    """字符二元组（S1 宽松匹配用，沿用 poc2 口径）。"""
    if len(text) <= 1:
        return frozenset({text} if text else ())
    return frozenset(text[i : i + 2] for i in range(len(text) - 1))


def soft_contains(a: str, b: str) -> bool:
    """宽松相等：全等 / 互相包含 / 二元组 Jaccard ≥ 0.6。"""
    na, nb = norm_value(a), norm_value(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    ba, bb = bigrams(na), bigrams(nb)
    return bool(ba and bb) and len(ba & bb) / len(ba | bb) >= 0.6


def s1_field_f1(pred: list[dict[str, Any]], gold: tuple[dict[str, str], ...]) -> dict[str, float]:
    """S1 候选级 F1：谓词等 + 主/宾宽松匹配，贪心一对一指派；返回 P/R/F1。"""
    used: set[int] = set()
    matched = 0
    for p in pred:
        p_pred = norm_value(str(p.get("predicate", "")))
        for gi, g in enumerate(gold):
            if gi in used:
                continue
            if p_pred != norm_value(str(g.get("predicate", ""))):
                continue
            if p_pred:  # relation/attribute：主宾均需匹配
                if soft_contains(str(p.get("name", "")), g["name"]) and soft_contains(
                    str(p.get("object", "")), g["object"]
                ):
                    used.add(gi)
                    matched += 1
                    break
            elif soft_contains(str(p.get("name", "")), g["name"]):  # entity：仅主体
                used.add(gi)
                matched += 1
                break
    precision = matched / len(pred) if pred else 0.0
    recall = matched / len(gold) if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


def gold_to_flatten(obj: object, prefix: str = "") -> dict[str, str]:
    """嵌套结构 → path:value 平面字典（数组按下标展开）——S2 字段级 F1 口径。"""
    flat: dict[str, str] = {}
    if isinstance(obj, dict):
        for key, val in obj.items():
            flat.update(gold_to_flatten(val, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, list):
        for idx, val in enumerate(obj):
            flat.update(gold_to_flatten(val, f"{prefix}[{idx}]"))
    else:
        flat[prefix] = str(obj)
    return flat


def s2_field_f1(pred: dict[str, Any], gold: dict[str, Any]) -> dict[str, float]:
    """S2 字段级 F1：嵌套展平后按 path 配对、值按 value_match；confidence 主观字段不计分。

    注意：数组 path 按下标严格对位——模型输出动作条数/次序变化会自然掉分（真实结构对齐能力）。
    """
    pred_flat = {k: v for k, v in gold_to_flatten(pred).items() if k.rsplit(".", 1)[-1] != "confidence"}
    gold_flat = {k: v for k, v in gold_to_flatten(gold).items() if k.rsplit(".", 1)[-1] != "confidence"}
    common = set(pred_flat) & set(gold_flat)
    matched = sum(1 for k in common if value_match(pred_flat[k], gold_flat[k]))
    precision = matched / len(pred_flat) if pred_flat else 0.0
    recall = matched / len(gold_flat) if gold_flat else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


# ---------------------------------------------------------------------------
# LLM 客户端（OpenAI 兼容；三模式 + 重试一次）
# ---------------------------------------------------------------------------

SYSTEM_PROMPT: Final = "你是电力配电网业务的结构化输出引擎。只输出一个 JSON 对象，不输出任何其他文本或解释。"

# S1 提示词带本体引导（对齐生产 extract 步 _EXTRACT_PROMPT_V1 的引导方式与 poc2 词汇表；
# 类型陈述→实体候选惯例，谓词/类型白名单与金标同源）
S1_USER_TEMPLATE: Final = """从下面的电力业务文本中抽取实体/关系候选，输出 JSON 对象。
规则：
1. 每条候选含 kind（entity|relation|attribute|event）、name、confidence（0 到 1 的小数）；
2. 谓词 predicate 只允许：hasStatus、inSection、dispatchedTo、crewLead、orderNo、
   confirmedAt、restoredAt、affectsFeeder；
3. 实体类型 ontology_class 只允许：馈线、配变、区段、开关、抢修队、工单、用户；
4. 「某实体是某类」的类型陈述输出 kind=entity（填 name 与 ontology_class，不加 predicate）；
   其余事实输出 kind=relation（填 name、ontology_class、predicate、object）；
5. 只抽文本明确陈述的事实，禁止编造；实体名取原文写法；
6. 文本没有可抽取内容时输出 {"candidates": []}。

文本：
{text}

输出 JSON：{"candidates": [{"kind", "name", "ontology_class", "predicate", "object", "confidence": 0.9}]}"""

S2_USER_TEMPLATE: Final = """把下面的电力故障工单文本转成结构化 JSON 对象，字段固定为：
ticket（no、priority ∈ 高|中|低）、fault（feeder、section、status ∈ 故障停运|已恢复）、
crews（数组，每项 name、lead）、actions（数组，每项 type ∈ 隔离|抢修|送电|巡线、target）、confidence（0 到 1）。

工单文本：
{text}

只输出该 JSON 对象。"""


class ChatClient:
    """最小 OpenAI 兼容 chat 客户端：探测 + 模式预检 + 三模式结构化调用（记录 usage 与耗时）。"""

    def __init__(
        self,
        label: str,
        base_url: str,
        model: str,
        api_key: str | None,
        *,
        vendor_extras: bool,
        max_tokens: int,
    ) -> None:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.label = label
        self.model = model
        self._vendor_extras = vendor_extras  # vLLM 私有参数（云渠道关掉，避免 400）
        self._max_tokens = max_tokens
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=HTTP_TIMEOUT_S)
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def probe(self) -> tuple[bool, list[str]]:
        """端点探测：返回（可用性, 模型 id 列表）。"""
        try:
            resp = self._client.get("/models", timeout=PROBE_TIMEOUT_S)
        except httpx.HTTPError:
            return False, []
        if resp.status_code != 200:
            return False, []
        body = resp.json()
        return True, [str(m.get("id", "")) for m in body.get("data", [])]

    def preflight_mode(self, schema: dict[str, Any], mode: str) -> str:
        """模式预检：小调用探 response_format 支持度，返回 ok / unsupported(<说明) / error(<说明)。"""
        messages = [{"role": "user", "content": '只输出 JSON 对象 {"ok": true}'}]
        _, _, err = self.chat(messages, schema, mode)
        if not err:
            return "ok"
        if err.startswith("http_4"):
            return f"unsupported({err})"
        return f"error({err})"

    def chat(self, messages: list[dict[str, str]], schema: dict[str, Any] | None, mode: str) -> tuple[str, float, str]:
        """单次调用：返回（content, 延迟秒, 错误说明）。mode 决定 response_format。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": 0,
        }
        if self._vendor_extras:
            # vLLM 私有：温度 0 下防复述循环 + qwen3 思考关闭（服务端不识别时忽略）
            payload["repetition_penalty"] = 1.05
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "poc5_output", "schema": schema},
            }
        started = time.perf_counter()
        try:
            resp = self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            return "", time.perf_counter() - started, f"http_error: {exc}"
        elapsed = time.perf_counter() - started
        if resp.status_code != 200:
            return "", elapsed, f"http_{resp.status_code}: {resp.text[:200]}"
        body = resp.json()
        usage = body.get("usage") or {}
        if isinstance(usage, dict):
            self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.completion_tokens += int(usage.get("completion_tokens") or 0)
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return "", elapsed, "no_choices"
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "")
        if not content:  # 推理型渠道可能把答案放在 reasoning_content
            content = str(message.get("reasoning_content") or "")
        return content, elapsed, ""

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# 评测执行
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CellResult:
    """矩阵单格（渠道×模式×schema）三态计数 + 逐样例明细。"""

    model_label: str
    mode: str
    schema_name: str
    n_samples: int = 0
    n_first_pass: int = 0  # 首次尝试解析+校验双过（解析兜底救回且未重试也计首次）
    n_retry_pass: int = 0  # 重试一次后过（含解析失败重试、schema 未过重试）
    n_fail: int = 0  # 重试后仍不过或调用失败（不可消费）
    n_parse_recovered: int = 0  # 严格解析失败但稳健兜底救回（未重试）
    n_mode_unsupported: int = 0
    latencies: list[float] = field(default_factory=list)
    f1_scores: list[float] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """格级指标：三态通过率、F1 均值（解析成功样例）、延迟摘要。"""
        n = self.n_samples
        return {
            "model": self.model_label,
            "mode": self.mode,
            "schema": self.schema_name,
            "n_samples": n,
            "mode_unsupported": self.n_mode_unsupported,
            "schema_pass_rate": round(self.n_first_pass / n, 4) if n else 0.0,
            "schema_pass_rate_after_retry": round((self.n_first_pass + self.n_retry_pass) / n, 4) if n else 0.0,
            "n_first_pass": self.n_first_pass,
            "n_retry_pass": self.n_retry_pass,
            "fail": self.n_fail,
            "parse_recovered": self.n_parse_recovered,
            "field_f1_mean_over_parsed": round(statistics.fmean(self.f1_scores), 4) if self.f1_scores else None,
            "latency_s_first_attempt": _latency_summary(self.latencies),
        }


def _latency_summary(seconds: list[float]) -> dict[str, float] | None:
    """延迟摘要（首次尝试口径；空格返回 None）。"""
    if not seconds:
        return None
    ordered = sorted(seconds)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {
        "mean": round(statistics.fmean(ordered), 3),
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[idx], 3),
        "max": round(ordered[-1], 3),
    }


def _compute_f1(parsed: object, gold: object, f1_kind: str) -> dict[str, float]:
    """按 schema 类型分发 F1 计算。"""
    if f1_kind == "s1":
        pred: list[Any] = parsed.get("candidates", []) if isinstance(parsed, dict) else []
        pred_items = [p for p in pred if isinstance(p, dict)]
        return s1_field_f1(pred_items, gold)  # type: ignore[arg-type]
    if isinstance(parsed, dict):
        return s2_field_f1(parsed, gold)  # type: ignore[arg-type]
    return {"precision": 0.0, "recall": 0.0, "f1": 0.0}


def _retry_once(
    client: ChatClient,
    messages: list[dict[str, str]],
    bad_content: str,
    schema: dict[str, Any],
    mode: str,
    feedback: str,
) -> tuple[object | None, str, str]:
    """带失败反馈重试一次：返回（parsed, 新 content, 错误说明）。"""
    retry_messages = [
        *messages,
        {"role": "assistant", "content": bad_content[:500]},
        {"role": "user", "content": feedback},
    ]
    content2, _, err2 = client.chat(retry_messages, schema, mode)
    if err2:
        return None, "", err2
    parsed, _ = parse_by_mode(mode, content2)
    return parsed, content2, ""


def run_cell(
    client: ChatClient, mode: str, schema_name: str, schema: dict[str, Any], samples: list[SampleInput]
) -> CellResult:
    """跑一个矩阵格；样例 = [(doc_id, user_prompt, gold, f1_kind)]。解析失败/schema 未过 → 反馈重试一次。"""
    cell = CellResult(client.label, mode, schema_name)
    cell.n_samples = len(samples)
    for doc_id, user_prompt, gold, f1_kind in samples:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        content, elapsed, err = client.chat(messages, schema, mode)
        if err:
            if err.startswith("http_4"):
                cell.n_mode_unsupported += 1
                cell.details.append({"doc_id": doc_id, "error": err, "mode_unsupported": True})
            else:
                cell.n_fail += 1
                cell.details.append({"doc_id": doc_id, "error": err})
            continue
        cell.latencies.append(elapsed)
        parsed, parse_err = parse_by_mode(mode, content)
        retried = False
        if parsed is None:
            retried = True
            parsed, content, parse_err = _retry_once(
                client,
                messages,
                content,
                schema,
                mode,
                f"上面的输出不是合法 JSON 对象（{parse_err}）。请重新输出，只输出合法 JSON 对象，不要任何解释或 null。",
            )
            if parsed is None:
                cell.n_fail += 1
                cell.details.append({"doc_id": doc_id, "error": f"retry_{parse_err}"})
                continue
        if parse_err == "json_parse_recovered":
            cell.n_parse_recovered += 1
        errors = validate_schema(parsed, schema)
        if errors:
            retried = True
            feedback = "；".join(errors[:3])[:400]
            retry_feedback = f"上面的输出未通过 JSON Schema 校验：{feedback}。请修正后重新输出完整 JSON 对象。"
            parsed2, _content2, _ = _retry_once(client, messages, content, schema, mode, retry_feedback)
            if parsed2 is not None and not validate_schema(parsed2, schema):
                parsed, errors = parsed2, []
        if errors:
            cell.n_fail += 1
            cell.details.append(
                {"doc_id": doc_id, "schema_errors_first": errors[:3], "f1": _compute_f1(parsed, gold, f1_kind)}
            )
        elif retried:
            cell.n_retry_pass += 1
            cell.details.append({"doc_id": doc_id, "retried": True, "f1": _compute_f1(parsed, gold, f1_kind)})
        else:
            cell.n_first_pass += 1
            cell.details.append({"doc_id": doc_id, "f1": _compute_f1(parsed, gold, f1_kind)})
        cell.f1_scores.append(_compute_f1(parsed, gold, f1_kind)["f1"])
    return cell


class SampleInput(tuple[str, str, object, str]):
    """矩阵格样例输入 (doc_id, user_prompt, gold, f1_kind)；元组子类型便于传参与注解。"""


# ---------------------------------------------------------------------------
# 渠道解析：主=本地 vLLM；fallback=DeepSeek（key 取环境或 .env 注释态，标注来源）
# ---------------------------------------------------------------------------


def load_env_values() -> dict[str, str]:
    """读仓库根 .env 生效键值（跳过注释/空行）。"""
    values: dict[str, str] = {}
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    return values


def find_commented_key() -> str | None:
    """扫描 .env 注释行里的 OA_LLM_API_KEY=sk-...（注释态 fallback 渠道证据）。"""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and "OA_LLM_API_KEY=" in stripped:
            _, _, val = stripped.partition("=")
            val = val.strip()
            if val.startswith("sk-"):
                return val
    return None


def pick_cloud_model(ids: list[str]) -> str:
    """fallback 模型选择：优先 .env 现役名 deepseek-chat，缺席取首个可用 id（记录漂移）。"""
    if "deepseek-chat" in ids:
        return "deepseek-chat"
    return ids[0] if ids else "deepseek-chat"


def main(argv: list[str] | None = None) -> int:
    """入口：装样例 → 装渠道（探测+预检）→ 跑满矩阵 → 汇总落 JSON。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(
                stream, "reconfigure"
            ):  # TextIO 抽象面无 reconfigure(仅 TextIOWrapper);hasattr 兼运行时守卫与类型收窄
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="PoC5 LLM 结构化输出兼容矩阵 harness")
    parser.add_argument("--local-base-url", default=None, help="本地 vLLM 端点（缺省 .env OA_LLM_BASE_URL）")
    parser.add_argument("--local-model", default=None, help="本地模型名（缺省 .env OA_LLM_MODEL）")
    parser.add_argument("--cloud-base-url", default="https://api.deepseek.com", help="云 fallback 端点")
    parser.add_argument("--cloud-key", default=None, help="云渠道 key（缺省取 .env；注释态 key 亦识别）")
    parser.add_argument("--no-cloud", action="store_true", help="跳过云 fallback 渠道")
    parser.add_argument("--limit", type=int, default=None, help="每格只跑前 N 条样例（自检用）")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="JSON 结果输出路径")
    args = parser.parse_args(argv)

    env = load_env_values()
    local_base = args.local_base_url or env.get("OA_LLM_BASE_URL", "http://127.0.0.1:18001/v1")
    local_model = args.local_model or env.get("OA_LLM_MODEL", "local-main")

    s1 = load_s1_samples()
    s2 = build_s2_samples()
    if args.limit:
        s1 = s1[: args.limit]
        s2 = s2[: args.limit]
    s1_inputs: list[SampleInput] = [
        SampleInput((d.doc_id, S1_USER_TEMPLATE.replace("{text}", d.text), d.gold, "s1")) for d in s1
    ]
    s2_inputs: list[SampleInput] = [
        SampleInput((d.doc_id, S2_USER_TEMPLATE.replace("{text}", d.text), d.gold, "s2")) for d in s2
    ]

    channels: list[ChatClient] = []
    local = ChatClient("local", local_base, local_model, None, vendor_extras=True, max_tokens=MAX_TOKENS_LOCAL)
    ok, _ids = local.probe()
    if not ok:
        print(f"[跳过] 本地端点探测失败：{local_base}", flush=True)
        local.close()
    else:
        channels.append(local)
        print(f"[本地] {local_base} model={local_model}", flush=True)

    cloud_info: dict[str, Any] = {"tested": False}
    cloud_key = args.cloud_key or env.get("OA_LLM_API_KEY") or find_commented_key()
    if cloud_key and not args.no_cloud:
        cloud = ChatClient(
            "deepseek",
            args.cloud_base_url,
            "",
            cloud_key,
            vendor_extras=False,
            max_tokens=MAX_TOKENS_CLOUD,
        )
        ok_c, ids_c = cloud.probe()
        if not ok_c:
            print(f"[跳过] 云端点探测失败：{args.cloud_base_url}", flush=True)
            cloud.close()
            cloud_info = {"tested": False, "reason": "probe_failed", "base_url": args.cloud_base_url}
        else:
            cloud.model = pick_cloud_model(ids_c)
            key_source = "env(OA_LLM_API_KEY)" if env.get("OA_LLM_API_KEY") else "env_commented_fallback"
            cloud_info = {
                "tested": True,
                "models_offered": ids_c,
                "model_used": cloud.model,
                "key_source": key_source,
                "max_tokens": MAX_TOKENS_CLOUD,
            }
            channels.append(cloud)
            print(f"[云 fallback] {args.cloud_base_url} model={cloud.model}（key 来源：{key_source}）", flush=True)
    elif not cloud_key:
        cloud_info = {"tested": False, "reason": "no key in .env"}
        print("[跳过] 云渠道无 key，标注未测", flush=True)
    else:
        cloud_info = {"tested": False, "reason": "--no-cloud"}
        print("[跳过] --no-cloud", flush=True)

    # 模式预检（response_format 支持度=兼容矩阵事实）
    preflight: dict[str, dict[str, str]] = {}
    for client in channels:
        preflight[client.label] = {}
        for mode in MODES:
            if mode == "prompt":
                preflight[client.label][mode] = "ok"
                continue
            status = client.preflight_mode(_EXTRACT_SCHEMA_V1, mode)
            preflight[client.label][mode] = status
            print(f"[预检] {client.label} × {mode}: {status}", flush=True)

    cells: list[CellResult] = []
    for client in channels:
        for mode, (schema_name, schema) in product(MODES, SCHEMAS.items()):
            inputs = s1_inputs if schema_name.startswith("S1") else s2_inputs
            if preflight[client.label][mode].startswith("unsupported"):
                ghost = CellResult(client.label, mode, schema_name)
                ghost.n_samples = len(inputs)
                ghost.n_mode_unsupported = len(inputs)
                cells.append(ghost)
                print(f"[skip] {client.label} × {mode} × {schema_name}（模式不支持，整格跳过）", flush=True)
                continue
            print(f"[run] {client.label} × {mode} × {schema_name}（{len(inputs)} 样例）", flush=True)
            cells.append(run_cell(client, mode, schema_name, schema, inputs))

    for client in channels:
        client.close()

    summaries = [cell.summary() for cell in cells]
    print("\n=== 矩阵汇总 ===", flush=True)
    for s in summaries:
        print(json.dumps(s, ensure_ascii=False), flush=True)

    result = {
        "meta": {
            "generated_at": datetime.now(UTC).isoformat(),
            "poc": "PoC⑤ LLM 结构化输出兼容矩阵",
            "modes": list(MODES),
            "schemas": {
                "S1_extract_v1": (
                    "services/kb/business/kb_extraction.py _EXTRACT_SCHEMA_V1（逐字段复制）；"
                    "提示词带谓词/类型白名单（对齐 poc2 词汇表）"
                ),
                "S2_ticket_v1": "自造嵌套 schema（工单结构化：嵌套对象×2+对象数组×2+enum/数值）",
            },
            "n_samples_s1": len(s1),
            "n_samples_s2": len(s2),
            "f1_rule": (
                "S1=候选级（谓词等+主/宾宽松匹配 soft_contains，贪心一对一）；"
                "S2=嵌套展平 path 严格对位+值等/中文包含（confidence 主观字段不计分）；"
                "均对自造真值；均值口径=解析成功样例"
            ),
            "verdict_rule": (
                "三态：first_pass=首次解析+校验双过（兜底救回未重试计首次）；retry_pass=重试一次后过；"
                "fail=重试后仍不过/调用失败（不可消费）；schema_pass_rate_after_retry=(first+retry)/n"
            ),
            "latency_note": (
                "本地格与云端格延迟不可直接互比（GPU 本地 vs 云网络+不同模型+max_tokens 不同）；同渠道内跨模式可比"
            ),
            "max_tokens": {"local": MAX_TOKENS_LOCAL, "cloud": MAX_TOKENS_CLOUD},
        },
        "channels": {"local": {"base_url": local_base, "model": local_model}, "cloud_fallback": cloud_info},
        "mode_preflight": preflight,
        "token_usage": {
            c.label: {"prompt_tokens": c.prompt_tokens, "completion_tokens": c.completion_tokens} for c in channels
        },
        "summary": summaries,
        "cells_detail": [
            {"model": c.model_label, "mode": c.mode, "schema": c.schema_name, "details": c.details} for c in cells
        ],
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[落盘] {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
