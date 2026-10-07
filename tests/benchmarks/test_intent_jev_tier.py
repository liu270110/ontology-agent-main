# tests/benchmarks/test_intent_jev_tier.py
"""intent 套件 A2=jev 档测试（docs/Agent/17 §1 批次 A，红队 E1「GLiNER 装了没接线」闭环）。

覆盖：
- **金标 schema 兼容**：A2 判定输出走 LLM 档同构 JSON 契约 → metrics.parse_prediction 可解
  → per_item 行键与 A0/A1 完全一致（orsi_link/release-eval 聚合面零特判）；
- 判定失败/超时 = parse_error 计入分母（与 LLM 档调用失败同口径，不静默挽救）；
- jev_vs_llm 差值列三键（jev_accuracy/jev_vs_llm_gain/jev_latency_vs_llm）计算口径；
- 明细块 _jev_vs_llm_block 结构（对照=A1 + vs_a0 双基线）；
- 档跳过（jev_tier_enabled=False）→ tiers 无 a2 + 三键置 None（历史曲线不掺空档）。

零真模型零网络（伪 jev 引擎桩 + 纯函数；AAA + 中文命名，test_intent_* 同款）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from benchmarks.suites.intent import metrics as m
from benchmarks.suites.intent.config import IntentBenchSettings
from benchmarks.suites.intent.dataset import load_dataset
from benchmarks.suites.intent.runner import _flat_metrics, _jev_vs_llm_block, run_jev_tier

# ── 构造器 ─────────────────────────────────────────────────────────────────────


class _FakeJevEngine:
    """伪 jev 引擎（detect 协议形；可注入逐条动作序与故障形态）。"""

    def __init__(
        self,
        *,
        actions: list[str] | None = None,
        confidences: list[float] | None = None,
        fail_on: set[int] | None = None,
        timeout_on: set[int] | None = None,
    ) -> None:
        self._actions = actions or ["file_read"]
        self._confidences = confidences or [0.9]
        self._fail_on = fail_on or set()  # 依赖缺失形态（JevUnavailableError）
        self._timeout_on = timeout_on or set()  # 超时形态（TimeoutError）
        self.queries: list[str] = []

    async def detect(self, text: str) -> Any:
        from services.agent.business.capabilities.jev.engine import JevDetection, JevUnavailableError

        idx = len(self.queries)
        self.queries.append(text)
        if idx in self._fail_on:
            raise JevUnavailableError("依赖缺失")
        if idx in self._timeout_on:
            raise TimeoutError()
        i = min(idx, len(self._actions) - 1)
        return JevDetection(
            action=self._actions[i],
            confidence=self._confidences[min(i, len(self._confidences) - 1)],
            intents=[],
            entities=[],
        )


def _settings(**over: Any) -> IntentBenchSettings:
    return IntentBenchSettings(**over)


_GOLDEN = [
    ("intent-001", "clear", "map", "file_read", "帮我读一下 README.md 的内容"),
    ("intent-041", "ambiguous", "map", "file_grep", "找一下哪里用了 retry"),
    ("intent-091", "out_of_scope", "reject", None, "今天上证指数多少"),
]


# ── 金标 schema 兼容（A2 与 LLM 档同契约）─────────────────────────────────────


def test_a2_判定输出可被llm档同口径解析():
    # Arrange：伪引擎三判定（file_read 0.9 / file_grep 0.8 / out_of_scope 0.0）
    from benchmarks.suites.intent.dataset import GoldenItem

    engine = _FakeJevEngine(actions=["file_read", "file_grep", "out_of_scope"], confidences=[0.9, 0.8, 0.0])
    items = [
        GoldenItem(id=i, query=q, expected_action=a, ambiguity_level=lv, notes="", expectation=ex)
        for i, lv, ex, a, q in _GOLDEN
    ]
    # Act
    verdicts, detections = asyncio.run(run_jev_tier(items, engine, _settings()))
    # Assert：per_item 行键与 A0/A1 完全一致（_per_item_rows 形态；orsi_link 聚合零特判）
    assert set(verdicts[0].__dataclass_fields__) == set(m.ItemVerdict.__dataclass_fields__)
    assert [v.correct for v in verdicts] == [True, True, False]  # map 条命中；reject 条口径=rejected
    assert verdicts[2].rejected is True  # 显式 out_of_scope 判拒（reject 口径）
    assert all(v.parse_error is False for v in verdicts)
    assert len(detections) == 3 and detections[0]["item_id"] == "intent-001"
    assert engine.queries[0] == "帮我读一下 README.md 的内容"  # 直调引擎（不走 LLM 提示）


def test_a2_per_item行键与llm档逐键一致():
    # Arrange：同一 ItemVerdict 类型承载三档——类型层面 schema 兼容的直接证据
    a2_like = m.score_item(
        item_id="x",
        ambiguity_level="clear",
        expectation="map",
        expected_action="file_read",
        raw_output='{"action": "file_read", "confidence": 0.9, "raw_spans_noisy": false}',
        confidence_floor=0.5,
    )
    llm_like = m.score_item(
        item_id="x",
        ambiguity_level="clear",
        expectation="map",
        expected_action="file_read",
        raw_output='{"action": "file_read", "confidence": 0.9}',
        confidence_floor=0.5,
    )
    # Act / Assert：附加键被解析侧忽略，两形态判分一致（契约兼容）
    assert a2_like.correct and llm_like.correct
    assert a2_like.predicted_action == llm_like.predicted_action == "file_read"
    assert a2_like.confidence == llm_like.confidence == 0.9


def test_a2_引擎不可用与超时_parse_error计入分母():
    # Arrange：第 0 条依赖缺失、第 1 条超时、第 2 条正常
    from benchmarks.suites.intent.dataset import GoldenItem

    engine = _FakeJevEngine(actions=["file_read"], fail_on={0}, timeout_on={1})
    items = [
        GoldenItem(id=i, query=q, expected_action=a, ambiguity_level=lv, notes="", expectation=ex)
        for i, lv, ex, a, q in _GOLDEN
    ]
    # Act
    verdicts, detections = asyncio.run(run_jev_tier(items, engine, _settings()))
    # Assert：失败两条=无输出按 parse_error 计入分母（与 LLM 档网络失败同口径）
    assert verdicts[0].parse_error and verdicts[1].parse_error and not verdicts[2].parse_error
    assert detections[0] == {} and detections[1] == {}  # 失败条无明细（不造假）
    got = m.aggregate(verdicts)
    assert got.parse_error_count == 2


# ── jev_vs_llm 差值列（E1 数据答案口径）───────────────────────────────────────


def _verdict(action: str, *, conf: float | None = 0.9) -> m.ItemVerdict:
    conf_part = "" if conf is None else f', "confidence": {conf}'
    raw = f'{{"action": "{action}"{conf_part}}}'
    return m.score_item(
        item_id="x", ambiguity_level="clear", expectation="map", expected_action="file_read",
        raw_output=raw, confidence_floor=0.5,
    )


def test_平铺三键_jev_accuracy_gain_latency():
    # Arrange：A0=2 条全对 / A1=2 条一对 / A2=2 条全对；A1 p50 高于 A2
    a0 = m.aggregate([_verdict("file_read"), _verdict("file_read")], latency_p50_ms=400.0)
    a1 = m.aggregate([_verdict("file_read"), _verdict("file_grep")], latency_p50_ms=400.0)
    a2 = m.aggregate([_verdict("file_read"), _verdict("file_read")], latency_p50_ms=50.0)
    # Act
    flat = _flat_metrics(a0, a1, a2)
    # Assert：三键存在且口径=jev−A1（对照=现役本体约束档），负值如实保留
    assert flat["jev_accuracy"] == 1.0
    assert flat["jev_vs_llm_gain"] == 0.5
    assert flat["jev_latency_vs_llm"] == -350.0


def test_平铺三键_档跳过时置None():
    # Arrange / Act
    flat = _flat_metrics(m.aggregate([]), m.aggregate([]), None)
    # Assert：跳过档不造假——三键显式 None（orsi_link 数值键过滤掉非数）
    assert flat["jev_accuracy"] is None
    assert flat["jev_vs_llm_gain"] is None
    assert flat["jev_latency_vs_llm"] is None


def test_明细块_对照a1加vs_a0双基线():
    # Arrange：A0 0.5 / A1 1.0 / A2 0.0（jev 全面落后=负值如实保留）
    a0 = m.aggregate([_verdict("file_read"), _verdict("file_grep")])
    a1 = m.aggregate([_verdict("file_read"), _verdict("file_read")])
    a2 = m.aggregate([_verdict("file_grep"), _verdict("file_grep")])
    # Act
    block = _jev_vs_llm_block(a0, a1, a2)
    # Assert：diff 对照=A1；vs_a0 双基线可审
    assert block["diff"]["reference"] == "a1"
    assert block["diff"]["intent_accuracy"] == 0.0 - 1.0
    assert block["vs_a0"]["intent_accuracy"] == 0.0 - 0.5
    assert set(block["latency_p50_ms"]) == {"jev", "a0", "a1"}


def test_明细块_跳过档返回None():
    assert _jev_vs_llm_block(m.aggregate([]), m.aggregate([]), None) is None


# ── 金标集 schema 兼容（A2 档直跑真金标装载路径）──────────────────────────────


def test_a2_直跑真金标装载路径_条数100():
    # Arrange / Act：金标集经既有 loader（schema 校验不过即失败）——A2 与 A0/A1 同集同契约
    items = load_dataset("v0")
    # Assert：同 100 金标（16 篇 §2 冻结分布；三档对照的分母基础）
    assert len(items) == 100
    from services.agent.business.capabilities.jev import action_names

    for it in items:  # 金标词表 ⊆ 平台行动类 ∪ ask_user（A2 判定的映射目标面）
        if it.expected_action is not None:
            assert it.expected_action in {*action_names(), "ask_user"}
