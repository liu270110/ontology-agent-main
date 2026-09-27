#!/usr/bin/env python3
"""PoC2 抽取置信度校准 harness（实施计划 13 篇 2.4；契约参考 services/platform/ports/model_port.py）。

流程：电力域抽取样例（自造文本 + 金标三元组）→ LLM 抽取（自报 confidence ∈ [0,1]）→
按 0.1 宽分桶做 reliability 表 → 计算 ECE（期望校准误差）→ 冻结人工终审阈值
（累计精确率 ≥ 0.95 的最低置信度档）与分层抽样率。

- LLM 通道：OpenAI 兼容端点（缺省取 services/platform/config.py 的 llm_base_url/llm_model/
  llm_api_key，import 失败则回退直读 .env 的 OA_LLM_*）；端点不可用时降级为
  「合成校准曲线演示」口径并在结果中显著标注（mode=synthetic_demo）。
- 双提示词变体对照：plain（自然自报）与 nudge（校准提示：要求按出错风险给出保守把握），
  检验「自报置信度」作为自动通过依据的可用性（模型/提示词绑定，变更须回归）。
- 匹配双口径（金标措辞差异敏感，沿用前批「严格+宽松并报」约定）：
  strict = NFKC+去空白+小写后 (subject, predicate, object) 全等；
  loose  = 谓词全等 且 主/宾「全等 或 互相包含 或 字符二元组 Jaccard ≥ 0.6」；
  候选按 confidence 降序贪心做金标一对一指派（一金标至多配一候选）。
- 本期小样例先行（金标 ~53 条），13 篇要求金标 ≥500 条，扩标登记为待办。

用法：
  python tools/poc2_confidence_calibration.py             # 自动探测 LLM，可用则实跑
  python tools/poc2_confidence_calibration.py --no-llm    # 强制合成演示口径
  python tools/poc2_confidence_calibration.py --limit 3   # 只跑前 3 篇（管线自检）

结果写 stdout（markdown 表）与 tools/poc2_calibration.json（含逐篇明细，全程可追溯）。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

PROMPT_VERSION_PLAIN: Final = "poc2-v1-plain"
PROMPT_VERSION_NUDGE: Final = "poc2-v2-nudge"
TARGET_PRECISION: Final = 0.95  # 人工终审阈值目标：累计精确率 ≥ 0.95 的最低置信度档
MIN_CUM_N: Final = 10  # 阈值判定要求累计候选数下限（小样例防噪声）
SAMPLE_RATE_FLOOR: Final = 0.05  # 阈值以上分层抽检比例下限（5%）
SAMPLE_RATE_CAP: Final = 0.50  # 分层抽检比例上限（50%，超出说明该档不宜自动通过）
SOFT_JACCARD: Final = 0.6  # 宽松口径的字符二元组 Jaccard 下限
DEFAULT_OUTPUT: Final = Path(__file__).resolve().parent / "poc2_calibration.json"
SYNTHETIC_SEED: Final = 42
HTTP_TIMEOUT_S: Final = 150.0
PROBE_TIMEOUT_S: Final = 5.0
MAX_TOKENS: Final = 700  # 短文本抽取足够；压低上限避免温度 0 下复述循环拖满长生成
MB: Final = 1024 * 1024

SYSTEM_PROMPT: Final = (
    "你是电力配电网信息抽取引擎。从给定文本中抽取事实三元组，只输出一个 JSON 对象，不输出任何其他文本或解释。"
)
_USER_TEMPLATE: Final = """从下面的电力业务文本中抽取事实三元组。

谓词只允许：{predicates}
实体类型只允许：{classes}
{extra}
要求：
1. 每条输出 subject（主语字面量）、subject_type、predicate、object（宾语字面量）、object_type、confidence；
2. confidence 是你对「这条三元组抽取正确」的自报把握，取值 [0,1] 的小数；
3. 只抽文本明确陈述的事实，禁止推断或编造；实体名与时间取原文写法，不要增删字（如不要加电压等级前缀）。

文本：
{text}

输出 JSON：
{{"relations": [{{"subject": "...", "subject_type": "...", "predicate": "...", "object": "...", \
"object_type": "...", "confidence": 0.0}}]}}"""
NUDGE_EXTRA: Final = (
    "注意：confidence 必须反映真实出错风险——对谓词选择、实体边界、别名写法没有把握时给出明显更低的值，\n"
    "不要全部给 1.0；请把你认为可能出错的三元组和很有把握的三元组区分开。\n"
)

ALLOWED_PREDICATES: Final = (
    "rdf:type",
    "hasStatus",
    "affectsFeeder",
    "inSection",
    "dispatchedTo",
    "crewLead",
    "orderNo",
    "confirmedAt",
    "restoredAt",
)
ALLOWED_CLASSES: Final = ("馈线", "配变", "区段", "开关", "抢修队", "工单", "用户")


# ---------------------------------------------------------------------------
# 电力域抽取样例（自造文本 + 金标三元组；确定性，无随机）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoldTriple:
    """金标三元组（谓词/类型可带命名空间前缀，匹配时归一）。"""

    subject: str
    subject_type: str
    predicate: str
    object: str
    object_type: str


@dataclass(frozen=True, slots=True)
class SampleDoc:
    """一条抽取样例：文本 + 金标三元组集。"""

    doc_id: str
    text: str
    gold: tuple[GoldTriple, ...]


def _g(sub: str, st: str, pred: str, obj: str, ot: str = "") -> GoldTriple:
    """金标三元组构造简写（subject, subject_type, predicate, object[, object_type]）。

    匹配口径只用 (subject, predicate, object)，object_type 缺省为空串。
    """
    return GoldTriple(sub, st, pred, obj, ot)


SAMPLE_DOCS: Final[tuple[SampleDoc, ...]] = (
    SampleDoc(
        "fault-001",
        "3月2日14时20分，10kV滨河线153开关过流保护动作跳闸，滨河线全线失压。"
        "故障录波判断故障点位于滨河线东段。调度确认滨河线故障停运，"
        "停电范围为滨河线东段全部用户，区段内人民医院、纺织厂受影响。",
        (
            _g("滨河线153开关", "开关", "rdf:type", "开关", "开关"),
            _g("滨河线153开关", "开关", "inSection", "滨河线东段"),
            _g("滨河线", "馈线", "hasStatus", "故障停运"),
            _g("滨河线东段", "区段", "inSection", "滨河线"),
        ),
    ),
    SampleDoc(
        "fault-002",
        "调度通知：10kV朝阳线北段312开关因大风异物短路跳闸，朝阳线北段失电。"
        "故障工单GD-2026-0301已生成，抢修一班被派往朝阳线北段处理故障。",
        (
            _g("朝阳线312开关", "开关", "inSection", "朝阳线北段"),
            _g("朝阳线北段", "区段", "hasStatus", "失电"),
            _g("工单GD-2026-0301", "工单", "dispatchedTo", "抢修一班"),
            _g("工单GD-2026-0301", "工单", "orderNo", "GD-2026-0301", "文本"),
        ),
    ),
    SampleDoc(
        "fault-003",
        "故障报告：3月5日9时05分东环线工业区段978开关跳闸，东环线工业区段停电。"
        "经巡线确认绝缘子击穿，东环线故障。11时20分东环线工业区段恢复送电，"
        "现场处置班组为抢修二班，负责人张建国。",
        (
            _g("东环线978开关", "开关", "rdf:type", "开关", "开关"),
            _g("东环线工业区段", "区段", "inSection", "东环线"),
            _g("东环线", "馈线", "hasStatus", "故障"),
            _g("抢修二班", "抢修队", "crewLead", "张建国"),
            _g("东环线工业区段", "区段", "restoredAt", "11时20分"),
        ),
    ),
    SampleDoc(
        "dispatch-001",
        "调度日志 3月8日：西城线过负荷告警，西城线状态转为受控。"
        "为控制风险，纺织厂负荷由西城线切换至南郊线供电。南郊线状态正常。",
        (
            _g("西城线", "馈线", "hasStatus", "受控"),
            _g("纺织厂", "用户", "inSection", "西城线"),
            _g("南郊线", "馈线", "hasStatus", "正常"),
        ),
    ),
    SampleDoc(
        "dispatch-002",
        "调度日志 3月11日：开发区线517开关检修停电，开发区线工业区段自8时30分停电。"
        "操作完成后开发区线保持正常。停电事件于13时00分确认结束。",
        (
            _g("开发区线517开关", "开关", "inSection", "开发区线工业区段"),
            _g("开发区线工业区段", "区段", "confirmedAt", "13时00分"),
            _g("开发区线", "馈线", "hasStatus", "正常"),
        ),
    ),
    SampleDoc(
        "dispatch-003",
        "调度日志 3月14日：滨河线东段故障抢修完成，滨河线恢复正常，对应故障工单GD-2026-0301归档，归档时间14时10分。",
        (
            _g("滨河线", "馈线", "hasStatus", "正常"),
            _g("工单GD-2026-0301", "工单", "restoresOrder", "滨河线东段"),
            _g("工单GD-2026-0301", "工单", "restoredAt", "14时10分"),
        ),
    ),
    SampleDoc(
        "ticket-001",
        "操作票 P-2026-0042：任务为滨河线153开关由运行转检修。"
        "操作前滨河线153开关状态为运行。操作班组为抢修一班，负责人李卫东。",
        (
            _g("滨河线153开关", "开关", "hasStatus", "运行"),
            _g("抢修一班", "抢修队", "crewLead", "李卫东"),
        ),
    ),
    SampleDoc(
        "ticket-002",
        "操作票 P-2026-0045：朝阳线312开关由热备用转冷备用，操作班组为抢修三班。"
        "操作涉及朝阳线北段，操作前朝阳线北段带电。",
        (
            _g("朝阳线312开关", "开关", "hasStatus", "热备用"),
            _g("抢修三班", "抢修队", "rdf:type", "抢修队", "抢修队"),
            _g("朝阳线北段", "区段", "hasStatus", "带电"),
        ),
    ),
    SampleDoc(
        "ledger-001",
        "设备台账：城东1号配变容量400kVA，接于滨河线东段，当前状态正常。"
        "城东1号配变供电用户为实验中学。台账更新日期3月15日。",
        (
            _g("城东1号配变", "配变", "rdf:type", "配变", "配变"),
            _g("城东1号配变", "配变", "inSection", "滨河线东段"),
            _g("城东1号配变", "配变", "hasStatus", "正常"),
            _g("实验中学", "用户", "inSection", "滨河线东段"),
        ),
    ),
    SampleDoc(
        "ledger-002",
        "设备台账：开发区2号配变容量630kVA，接于开发区线工业区段，状态运行。"
        "开发区2号配变负责纺织厂供电。抢修三班负责人王志强。",
        (
            _g("开发区2号配变", "配变", "rdf:type", "配变", "配变"),
            _g("开发区2号配变", "配变", "inSection", "开发区线工业区段"),
            _g("开发区2号配变", "配变", "hasStatus", "运行"),
            _g("纺织厂", "用户", "inSection", "开发区线工业区段"),
            _g("抢修三班", "抢修队", "crewLead", "王志强"),
        ),
    ),
    SampleDoc(
        "analysis-001",
        "月度分析：3月滨河线共跳闸2次，均位于滨河线东段，故障停运累计4.5小时。"
        "目前滨河线状态正常，人民医院由滨河线东段供电，双回路改造后可靠性提升。",
        (
            _g("滨河线东段", "区段", "inSection", "滨河线"),
            _g("滨河线", "馈线", "hasStatus", "正常"),
            _g("人民医院", "用户", "inSection", "滨河线东段"),
        ),
    ),
    SampleDoc(
        "analysis-002",
        "分析报告：朝阳线3月16日发生风偏放电故障，朝阳线故障停运55分钟。"
        "故障工单GD-2026-0305派发抢修三班，12时30分朝阳线恢复正常。",
        (
            _g("朝阳线", "馈线", "hasStatus", "故障停运"),
            _g("工单GD-2026-0305", "工单", "dispatchedTo", "抢修三班"),
            _g("朝阳线", "馈线", "restoredAt", "12时30分"),
        ),
    ),
)


# ---------------------------------------------------------------------------
# 归一与匹配（strict / loose 双口径 + 贪心一对一指派）
# ---------------------------------------------------------------------------

_NS_SPLIT: Final = re.compile(r"[#/:：]")
_WS: Final = re.compile(r"\s+")


def norm_literal(text: str) -> str:
    """字面量归一：NFKC + 去全部空白 + 小写。"""
    return _WS.sub("", unicodedata.normalize("NFKC", text)).lower()


def norm_term(term: str) -> str:
    """谓词/类型归一：去命名空间前缀取末段后按字面量归一。"""
    return norm_literal(_NS_SPLIT.split(term.strip())[-1])


def match_key(subject: str, predicate: str, obj: str) -> tuple[str, str, str]:
    return norm_literal(subject), norm_term(predicate), norm_literal(obj)


def _bigrams(text: str) -> frozenset[str]:
    if len(text) <= 1:
        return frozenset({text} if text else ())
    return frozenset(text[i : i + 2] for i in range(len(text) - 1))


def soft_literal_match(a: str, b: str) -> bool:
    """宽松字面量匹配：全等 / 互相包含 / 字符二元组 Jaccard ≥ SOFT_JACCARD。"""
    if a == b:
        return True
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    bag_a, bag_b = _bigrams(a), _bigrams(b)
    if not bag_a or not bag_b:
        return False
    return len(bag_a & bag_b) / len(bag_a | bag_b) >= SOFT_JACCARD


@dataclass(slots=True)
class Candidate:
    """LLM 抽取候选（confidence 为模型自报；matched 由金标比对回填）。"""

    subject: str
    subject_type: str
    predicate: str
    object: str
    object_type: str
    confidence: float
    doc_id: str = ""
    matched_strict: bool = False
    matched_loose: bool = False


def assign_matches(doc: SampleDoc, candidates: list[Candidate]) -> None:
    """按 confidence 降序贪心做金标一对一指派，回填 matched_strict / matched_loose。

    strict：三元组全等；loose：谓词全等 + 主/宾宽松匹配。一金标在任一口径下至多配一候选。
    """
    gold_keys = [match_key(t.subject, t.predicate, t.object) for t in doc.gold]
    used_strict: set[int] = set()
    used_loose: set[int] = set()
    for cand in sorted(candidates, key=lambda c: c.confidence, reverse=True):
        sk, pk, ok = match_key(cand.subject, cand.predicate, cand.object)
        for idx, (gsk, gpk, gok) in enumerate(gold_keys):
            if pk != gpk:
                continue
            if idx not in used_strict and sk == gsk and ok == gok:
                cand.matched_strict = True
                used_strict.add(idx)
            if idx not in used_loose and soft_literal_match(sk, gsk) and soft_literal_match(ok, gok):
                cand.matched_loose = True
                used_loose.add(idx)


# ---------------------------------------------------------------------------
# LLM 客户端（OpenAI 兼容；对齐 ModelPort 语义：结构化输出 + 显式超时）
# ---------------------------------------------------------------------------


class ChatClient:
    """最小 OpenAI 兼容 chat 客户端：探测 + JSON 抽取调用（记录 usage 与耗时）。"""

    def __init__(self, base_url: str, model: str, api_key: str | None, timeout_s: float = HTTP_TIMEOUT_S) -> None:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s)
        self.model = model
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.latencies: list[float] = []

    def probe(self) -> bool:
        """端点可用性探测（GET /models，短超时）。"""
        try:
            resp = self._client.get("/models", timeout=PROBE_TIMEOUT_S)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def extract_relations(self, text: str, *, nudge: bool) -> tuple[list[Candidate], str]:
        """单篇抽取调用；返回（候选列表, 错误说明）。失败自动重试一次，解析失败返回空列表与错误串。"""
        user = _USER_TEMPLATE.format(
            predicates="、".join(ALLOWED_PREDICATES),
            classes="、".join(ALLOWED_CLASSES),
            extra=NUDGE_EXTRA if nudge else "",
            text=text,
        )
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
            # 温度 0 下小模型可能复述循环；repetition_penalty 轻度抑制（vLLM 原生支持，其余服务端忽略）
            "repetition_penalty": 1.05,
            # qwen3 系思考模式关闭（vLLM chat_template_kwargs；无效参数被服务端忽略不报错）
            "chat_template_kwargs": {"enable_thinking": False},
        }
        last_err = ""
        for _attempt in range(2):
            started = time.perf_counter()
            try:
                resp = self._client.post("/chat/completions", json=payload)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                last_err = f"http_error: {exc}"
                continue
            self.latencies.append(time.perf_counter() - started)
            self.calls += 1
            body: dict[str, object] = resp.json()
            usage = body.get("usage") or {}
            if isinstance(usage, dict):
                self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.completion_tokens += int(usage.get("completion_tokens") or 0)
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices:
                last_err = "no_choices"
                continue
            message: dict[str, object] = choices[0].get("message") or {}  # type: ignore[union-attr]
            content = str(message.get("content") or "")
            parsed, err = _parse_relations_json(content)
            if err:
                last_err = err
                continue
            return _to_candidates(parsed), ""
        return [], last_err

    def close(self) -> None:
        self._client.close()


_THINK: Final = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE: Final = re.compile(r"```(?:json)?\s*|\s*```")


def _parse_relations_json(content: str) -> tuple[object, str]:
    """从模型输出中稳健解析 relations JSON（剥 think 块/代码围栏/定位首尾括号）。"""
    cleaned = _THINK.sub("", content)
    cleaned = _FENCE.sub("", cleaned).strip()
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = cleaned.find(open_ch)
        end = cleaned.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1]), ""
            except json.JSONDecodeError:
                continue
    return [], f"json_parse_failed: {cleaned[:120]!r}"


def _to_candidates(parsed: object) -> list[Candidate]:
    """结构 → 候选列表；relations 缺失/形态不符按空处理（schema 校验不过=不可消费）。"""
    if isinstance(parsed, dict):
        parsed = parsed.get("relations", [])
    if not isinstance(parsed, list):
        return []
    out: list[Candidate] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        out.append(
            Candidate(
                subject=str(item.get("subject", "")),
                subject_type=str(item.get("subject_type", "")),
                predicate=str(item.get("predicate", "")),
                object=str(item.get("object", "")),
                object_type=str(item.get("object_type", "")),
                confidence=min(max(conf, 0.0), 1.0),
            )
        )
    return out


# ---------------------------------------------------------------------------
# 配置解析：优先 services.platform.config，失败回退 .env 直读（tools 零平台依赖）
# ---------------------------------------------------------------------------


def resolve_llm_config() -> tuple[str, str, str | None, str]:
    """返回 (base_url, model, api_key, 来源说明)；未配置时 base_url 为空串。"""
    try:
        repo_root = str(Path(__file__).resolve().parent.parent)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)  # 脚本直跑时 sys.path 只有 tools/，需显式补仓库根
        from services.platform.config import get_settings

        s = get_settings()
        return (s.llm_base_url or "", s.llm_model, s.llm_api_key, "services.platform.config")
    except Exception:  # noqa: BLE001 — tools 场景：任何 import/加载失败都走 .env 回退
        env_path = Path(__file__).resolve().parent.parent / ".env"
        values: dict[str, str] = {}
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
        return (
            values.get("OA_LLM_BASE_URL", ""),
            values.get("OA_LLM_MODEL", "deepseek-chat"),
            values.get("OA_LLM_API_KEY") or None,
            ".env fallback",
        )


# ---------------------------------------------------------------------------
# 合成降级（端点不可用时的「合成演示」口径，显著标注）
# ---------------------------------------------------------------------------


def synthetic_run(docs: tuple[SampleDoc, ...], seed: int = SYNTHETIC_SEED) -> list[Candidate]:
    """生成带内在正确性的合成候选：conf 越高正确率越高（确定性种子），并注入少量幻觉候选。

    产物仅供演示计算链路，不得作为校准结论引用。
    """
    rng = random.Random(seed)
    candidates: list[Candidate] = []
    for doc in docs:
        for gold in doc.gold:
            base = rng.choice((0.35, 0.55, 0.75, 0.9))
            conf = min(max(rng.gauss(base, 0.08), 0.05), 0.99)
            # 正确概率随自报置信度上升（模拟欠校准：高端略乐观、中端略悲观）
            p_correct = 0.55 + 0.42 * conf**2
            correct = rng.random() < p_correct
            obj = gold.object if correct else f"{gold.object}（幻觉）"
            candidates.append(
                Candidate(gold.subject, gold.subject_type, gold.predicate, obj, gold.object_type, conf, doc.doc_id)
            )
    return candidates


# ---------------------------------------------------------------------------
# 校准度量：分桶 / ECE / 阈值与抽样率
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Bucket:
    """单个置信度桶的 reliability 统计。"""

    label: str
    lo: float
    hi: float
    n: int = 0
    correct: int = 0
    conf_sum: float = 0.0

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0

    @property
    def avg_conf(self) -> float:
        return self.conf_sum / self.n if self.n else 0.0


def bucket_index(conf: float) -> int:
    """0.1 宽桶序号；conf=1.0 归入最后一桶。"""
    return min(int(conf * 10), 9)


def compute_reliability(candidates: list[Candidate], attr: str) -> list[Bucket]:
    """按置信度分桶统计桶内精确率与平均自报置信度；attr 取 matched_strict/matched_loose。"""
    buckets = [Bucket(f"{i / 10:.1f}-{(i + 1) / 10:.1f}", i / 10, (i + 1) / 10) for i in range(10)]
    for cand in candidates:
        b = buckets[bucket_index(cand.confidence)]
        b.n += 1
        b.correct += int(getattr(cand, attr))
        b.conf_sum += cand.confidence
    return buckets


def compute_ece(buckets: list[Bucket], total: int) -> float:
    """ECE = Σ (n_b/N)·|acc_b − avg_conf_b|。"""
    if total == 0:
        return 0.0
    return sum(b.n / total * abs(b.accuracy - b.avg_conf) for b in buckets if b.n)


@dataclass(slots=True)
class ThresholdPlan:
    """人工终审阈值与分层抽样方案。"""

    threshold: float | None = None  # 自动通过下界（None=无档达标，全部进人工）
    cum_precision_at_threshold: float | None = None
    auto_pass_rate: float = 0.0  # 阈值以上候选占比
    above_bins_rate: dict[str, float] = field(default_factory=dict)  # 桶 → 分层抽检比例
    overall_sample_rate: float = 0.0  # 阈值以上区间的总体抽检率
    review_queue_rate: float = 1.0  # 进人工终审队列比例（含阈值以下全检）


def plan_threshold(buckets: list[Bucket], target: float = TARGET_PRECISION) -> ThresholdPlan:
    """自最高桶向下累计精确率，找 ≥target 的最低置信度档；并给出分层抽样率。"""
    ordered = [b for b in buckets if b.n]
    plan = ThresholdPlan()
    cum_n = 0
    cum_correct = 0
    for b in reversed(ordered):
        cum_n += b.n
        cum_correct += b.correct
        precision = cum_correct / cum_n
        if precision >= target and cum_n >= MIN_CUM_N:
            plan.threshold = b.lo
            plan.cum_precision_at_threshold = precision
    above = [b for b in ordered if plan.threshold is not None and b.lo >= plan.threshold - 1e-9]
    above_n = sum(b.n for b in above)
    total = sum(b.n for b in ordered)
    plan.auto_pass_rate = above_n / total if total else 0.0
    plan.review_queue_rate = 1.0 - plan.auto_pass_rate
    weighted = 0.0
    for b in above:
        rate = min(max(1.0 - b.accuracy, SAMPLE_RATE_FLOOR), SAMPLE_RATE_CAP)
        plan.above_bins_rate[b.label] = rate
        weighted += b.n * rate
    plan.overall_sample_rate = weighted / above_n if above_n else 0.0
    return plan


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------


def render_variant_markdown(title: str, buckets: list[Bucket], ece: float, plan: ThresholdPlan) -> str:
    lines = [
        f"### {title}",
        "",
        "| 置信度桶 | 样本数 | 桶内精确率 | 平均自报置信度 |",
        "| --- | --- | --- | --- |",
    ]
    for b in buckets:
        row_n = str(b.n) if b.n else "0"
        row_acc = f"{b.accuracy:.3f}" if b.n else "-"
        row_conf = f"{b.avg_conf:.3f}" if b.n else "-"
        lines.append(f"| {b.label} | {row_n} | {row_acc} | {row_conf} |")
    lines.append(f"| **ECE** | — | {ece:.4f} | — |")
    threshold_txt = str(plan.threshold) if plan.threshold is not None else "无达标档（全部人工终审）"
    lines += [
        "",
        f"- 人工终审阈值建议：**{threshold_txt}**"
        + (f"（该档累计精确率 {plan.cum_precision_at_threshold:.3f}）" if plan.cum_precision_at_threshold else ""),
        f"- 自动通过占比：{plan.auto_pass_rate:.1%}；进人工队列占比：{plan.review_queue_rate:.1%}",
        f"- 阈值以上分层抽检率（按 1−桶精确率，下限 {SAMPLE_RATE_FLOOR:.0%} 上限 {SAMPLE_RATE_CAP:.0%}）："
        + ("，".join(f"{k}={v:.0%}" for k, v in plan.above_bins_rate.items()) or "不适用"),
        f"- 总体抽样率：{plan.overall_sample_rate:.1%}",
    ]
    return "\n".join(lines)


@dataclass(slots=True)
class VariantResult:
    """单个提示词变体的完整校准结果。"""

    name: str
    prompt_version: str
    candidates: list[Candidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    n_parsed_fail: int = 0


def evaluate_variant(
    variant: VariantResult, docs: tuple[SampleDoc, ...], attr: str
) -> tuple[list[Bucket], float, ThresholdPlan]:
    """对单变体计算 reliability / ECE / 阈值方案。"""
    buckets = compute_reliability(variant.candidates, attr)
    ece = compute_ece(buckets, len(variant.candidates))
    plan = plan_threshold(buckets)
    return buckets, ece, plan


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_llm_variant(
    client: ChatClient, docs: tuple[SampleDoc, ...], name: str, prompt_version: str, *, nudge: bool
) -> VariantResult:
    """跑一个提示词变体：逐篇抽取 + 双口径金标指派。"""
    variant = VariantResult(name=name, prompt_version=prompt_version)
    for doc in docs:
        got, err = client.extract_relations(doc.text, nudge=nudge)
        if err:
            variant.errors.append(f"{doc.doc_id}: {err}")
            variant.n_parsed_fail += 0 if err.startswith("http_error") else 1
        for cand in got:
            cand.doc_id = doc.doc_id
            variant.candidates.append(cand)
        assign_matches(doc, variant.candidates)
        n_hit = sum(1 for c in variant.candidates if c.doc_id == doc.doc_id and c.matched_strict)
        print(f"  [{name}] {doc.doc_id}: 候选 {len(got)} 金标 {len(doc.gold)} strict命中 {n_hit}", flush=True)
    return variant


def variant_payload(
    variant: VariantResult, buckets: list[Bucket], ece: float, plan: ThresholdPlan
) -> dict[str, object]:
    return {
        "name": variant.name,
        "prompt_version": variant.prompt_version,
        "n_candidates": len(variant.candidates),
        "n_strict_tp": sum(c.matched_strict for c in variant.candidates),
        "n_loose_tp": sum(c.matched_loose for c in variant.candidates),
        "precision_strict": round(sum(c.matched_strict for c in variant.candidates) / len(variant.candidates), 4)
        if variant.candidates
        else 0.0,
        "precision_loose": round(sum(c.matched_loose for c in variant.candidates) / len(variant.candidates), 4)
        if variant.candidates
        else 0.0,
        "ece_strict": round(ece, 4),
        "buckets_strict": [
            {"label": b.label, "n": b.n, "accuracy": round(b.accuracy, 4), "avg_confidence": round(b.avg_conf, 4)}
            for b in buckets
        ],
        "threshold_plan_strict": {
            "auto_pass_threshold": plan.threshold,
            "cum_precision_at_threshold": plan.cum_precision_at_threshold,
            "auto_pass_rate": round(plan.auto_pass_rate, 4),
            "review_queue_rate": round(plan.review_queue_rate, 4),
            "stratified_sample_rates": {k: round(v, 4) for k, v in plan.above_bins_rate.items()},
            "overall_sample_rate": round(plan.overall_sample_rate, 4),
        },
        "errors": variant.errors,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="PoC2 抽取置信度校准 harness")
    parser.add_argument("--no-llm", action="store_true", help="强制合成演示口径（不调 LLM）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 篇样例（自检用）")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="JSON 结果输出路径")
    args = parser.parse_args(argv)

    docs = SAMPLE_DOCS[: args.limit] if args.limit else SAMPLE_DOCS
    gold_total = sum(len(d.gold) for d in docs)
    base_url, model, api_key, cfg_source = resolve_llm_config()
    mode = "synthetic_demo"
    variants: list[VariantResult] = []
    client_metrics: ChatClient | None = None

    client: ChatClient | None = None
    if not args.no_llm and base_url:
        client = ChatClient(base_url, model, api_key)
        if not client.probe():
            errors = [f"probe failed: {base_url}"]
            client.close()
            client = None
            print(f"[降级] 端点探测失败：{errors[0]}", flush=True)
        else:
            mode = "llm_live"
            print(f"[LLM] {base_url} model={model}（配置来源：{cfg_source}）", flush=True)

    if client is not None:
        client_metrics = client
        variants.append(run_llm_variant(client, docs, "plain", PROMPT_VERSION_PLAIN, nudge=False))
        variants.append(run_llm_variant(client, docs, "nudge", PROMPT_VERSION_NUDGE, nudge=True))
        client.close()
    else:
        cand = synthetic_run(docs)
        variants.append(VariantResult(name="plain(synthetic)", prompt_version="synthetic-demo", candidates=cand))
        print("[降级] LLM 不可用或 --no-llm：合成演示口径（结果不得作为校准结论引用）", flush=True)

    # 金标召回（strict 口径；FN 只计整体召回，不进桶）
    strict_hits = 0
    for doc in docs:
        doc_cands = [c for c in variants[0].candidates if c.doc_id == doc.doc_id]
        gold_keys = {match_key(t.subject, t.predicate, t.object) for t in doc.gold}
        strict_hits += sum(
            1 for k in gold_keys if any(match_key(c.subject, c.predicate, c.object) == k for c in doc_cands)
        )
    recall = strict_hits / gold_total if gold_total else 0.0

    print("", flush=True)
    rendered: list[str] = []
    variant_json: list[dict[str, object]] = []
    for variant in variants:
        buckets, ece, plan = evaluate_variant(variant, docs, "matched_strict")
        rendered.append(render_variant_markdown(f"变体 {variant.name}（strict 口径）", buckets, ece, plan))
        variant_json.append(variant_payload(variant, buckets, ece, plan))
        loose_buckets, loose_ece, _ = evaluate_variant(variant, docs, "matched_loose")
        variant_json[-1]["ece_loose"] = round(loose_ece, 4)
        variant_json[-1]["buckets_loose"] = [
            {"label": b.label, "n": b.n, "accuracy": round(b.accuracy, 4), "avg_confidence": round(b.avg_conf, 4)}
            for b in loose_buckets
        ]

    result = {
        "meta": {
            "generated_at": datetime.now(UTC).isoformat(),
            "base_url": base_url,
            "model": model if client else None,
            "config_source": cfg_source,
            "mode": mode,
            "target_precision": TARGET_PRECISION,
            "n_docs": len(docs),
            "n_gold": gold_total,
            "recall_strict_plain": round(recall, 4),
            "match_rule": (
                "strict=NFKC+去空白+小写 (s,p,o) 全等；loose=谓词全等+主/宾含混匹配"
                "（全等/互相包含/二元组 Jaccard≥0.6）；confidence 降序贪心一对一指派"
            ),
            "llm_calls": client_metrics.calls if client_metrics else 0,
            "llm_prompt_tokens": client_metrics.prompt_tokens if client_metrics else 0,
            "llm_completion_tokens": client_metrics.completion_tokens if client_metrics else 0,
            "llm_latency_median_s": (
                round(statistics.median(client_metrics.latencies), 3)
                if client_metrics and client_metrics.latencies
                else None
            ),
            "note": (
                "合成演示口径：端点不可用时的计算链路演示，不得作为校准结论引用"
                if mode == "synthetic_demo"
                else "LLM 实跑口径（模型/提示词绑定，变更须按架构 08 篇 §7.2 回归）"
            ),
        },
        "variants": variant_json,
        "per_doc": [
            {
                "doc_id": d.doc_id,
                "n_gold": len(d.gold),
                **{
                    v.name: {
                        "n_candidates": sum(1 for c in v.candidates if c.doc_id == d.doc_id),
                        "n_strict": sum(1 for c in v.candidates if c.doc_id == d.doc_id and c.matched_strict),
                        "n_loose": sum(1 for c in v.candidates if c.doc_id == d.doc_id and c.matched_loose),
                    }
                    for v in variants
                },
            }
            for d in docs
        ],
        "candidates": [
            {
                "variant": v.name,
                "doc_id": c.doc_id,
                "subject": c.subject,
                "subject_type": c.subject_type,
                "predicate": c.predicate,
                "object": c.object,
                "object_type": c.object_type,
                "confidence": round(c.confidence, 3),
                "matched_strict": c.matched_strict,
                "matched_loose": c.matched_loose,
            }
            for v in variants
            for c in v.candidates
        ],
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[落盘] {args.output}（{args.output.stat().st_size / MB:.2f}MB）", flush=True)
    print("\n\n".join(rendered))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
