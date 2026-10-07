"""intent 指标口径单测（四指标口径 = metrics.py 唯一事实源，本文件锁定其行为）。"""

from __future__ import annotations

from benchmarks.suites.intent.metrics import (
    aggregate,
    ontology_constraint_gain,
    parse_prediction,
    score_item,
)

_MAP = dict(item_id="intent-001", ambiguity_level="clear", expectation="map", expected_action="file_grep")
_CLARIFY = dict(
    item_id="intent-081", ambiguity_level="need_clarification", expectation="clarify", expected_action="ask_user"
)
_REJECT = dict(item_id="intent-091", ambiguity_level="out_of_scope", expectation="reject", expected_action=None)


class TestParsePrediction:
    """输出解析口径：剥 <think> + 首个平衡 JSON；契约违规 → None（parse_error）。"""

    def test_纯_json_直解(self):
        # Arrange / Act / Assert
        assert parse_prediction('{"action": "file_grep", "confidence": 0.9}') == parse_prediction(
            '{"action":"file_grep","confidence":0.9}'
        )
        parsed = parse_prediction('{"action": "file_grep", "confidence": 0.9}')
        assert parsed is not None and parsed.action == "file_grep" and parsed.confidence == 0.9

    def test_qwen3_思考块被剥除(self):
        # Arrange：qwen3 内联 <think> 后接 JSON（18001 实测输出形态）
        raw = '<think>\n推理过程含 {"action": "file_read"} 干扰项\n</think>\n{"action": "file_grep", "confidence": 0.8}'
        # Act
        parsed = parse_prediction(raw)
        # Assert：取 think 块之后的正文 JSON
        assert parsed is not None and parsed.action == "file_grep"

    def test_未闭合思考块无_json_记解析失败(self):
        # Arrange / Act / Assert：max_tokens 截断形态（思考未结束、正文 JSON 未产出）=契约违规，
        # 如实 parse_error（不静默挽救）
        assert parse_prediction("<think>\n半截思考无闭合，正文缺席") is None

    def test_无_json_与缺_action_均解析失败(self):
        # Arrange / Act / Assert
        assert parse_prediction("我觉得是 file_grep") is None
        assert parse_prediction('{"confidence": 0.9}') is None
        assert parse_prediction('{"action": "", "confidence": 0.9}') is None

    def test_confidence_缺失与越界钳位(self):
        # Arrange / Act
        no_conf = parse_prediction('{"action": "todo_read"}')
        clamped = parse_prediction('{"action": "todo_read", "confidence": 7}')
        # Assert：缺失记 None（仅影响越界低置信通路）；越界钳到 [0,1]
        assert no_conf is not None and no_conf.confidence is None
        assert clamped is not None and clamped.confidence == 1.0


class TestScoreItem:
    """单条判定口径：map/clarify/reject 三期望各按各自规则。"""

    def test_map_命中与未命中(self):
        # Arrange / Act
        hit = score_item(**_MAP, raw_output='{"action": "file_grep", "confidence": 0.9}', confidence_floor=0.5)
        miss = score_item(**_MAP, raw_output='{"action": "file_read", "confidence": 0.9}', confidence_floor=0.5)
        # Assert
        assert hit.correct and not miss.correct

    def test_map_解析失败不计正确(self):
        # Arrange / Act
        bad = score_item(**_MAP, raw_output="没有 JSON", confidence_floor=0.5)
        # Assert
        assert bad.parse_error and not bad.correct

    def test_clarify_以_ask_user_为触发口径(self):
        # Arrange / Act
        triggered = score_item(**_CLARIFY, raw_output='{"action": "ask_user", "confidence": 0.7}', confidence_floor=0.5)
        guessed = score_item(**_CLARIFY, raw_output='{"action": "file_edit", "confidence": 0.4}', confidence_floor=0.5)
        # Assert：猜错行动不算触发（低置信猜错也不算——澄清口径只认 ask_user）
        assert triggered.clarified and not guessed.clarified

    def test_reject_显式拒与低置信映射均算拒(self):
        # Arrange
        explicit_raw = '{"action": "out_of_scope", "confidence": 0.9}'
        low_conf_raw = '{"action": "run_terminal", "confidence": 0.2}'
        high_conf_raw = '{"action": "run_terminal", "confidence": 0.8}'
        # Act
        explicit = score_item(**_REJECT, raw_output=explicit_raw, confidence_floor=0.5)
        low_conf = score_item(**_REJECT, raw_output=low_conf_raw, confidence_floor=0.5)
        high_conf_guess = score_item(**_REJECT, raw_output=high_conf_raw, confidence_floor=0.5)
        # Assert：拒/低置信映射=诚实出路；高置信硬猜=不拒
        assert explicit.rejected and low_conf.rejected and not high_conf_guess.rejected

    def test_reject_confidence_缺失不按低置信判拒(self):
        # Arrange / Act：无 confidence 时低置信通路不可判——只认显式拒
        no_conf = score_item(**_REJECT, raw_output='{"action": "run_terminal"}', confidence_floor=0.5)
        # Assert
        assert not no_conf.rejected


class TestAggregate:
    """聚合口径：三期望条各按各自分母；parse_error 留在分母内。"""

    def _v(self, base: dict, *, raw: str, floor: float = 0.5):
        return score_item(**base, raw_output=raw, confidence_floor=floor)

    def test_三率各按各自分母(self):
        # Arrange：map 2 条中 1 对；clarify 1 条触发；reject 1 条拒
        verdicts = [
            self._v(_MAP, raw='{"action": "file_grep", "confidence": 0.9}'),
            self._v(_MAP, raw='{"action": "file_read", "confidence": 0.9}'),
            self._v(_CLARIFY, raw='{"action": "ask_user", "confidence": 0.7}'),
            self._v(_REJECT, raw='{"action": "out_of_scope", "confidence": 0.9}'),
        ]
        # Act
        got = aggregate(verdicts)
        # Assert
        assert got.map_count == 2 and got.clarify_count == 1 and got.reject_count == 1
        assert got.intent_accuracy == 0.5
        assert got.clarification_trigger_rate == 1.0
        assert got.out_of_scope_reject_rate == 1.0

    def test_parse_error_留在分母内(self):
        # Arrange：map 2 条，其中 1 条解析失败
        verdicts = [
            self._v(_MAP, raw='{"action": "file_grep", "confidence": 0.9}'),
            self._v(_MAP, raw="乱输出"),
        ]
        # Act
        got = aggregate(verdicts)
        # Assert：0.5 而非 1.0——契约违规是行为的一部分
        assert got.parse_error_count == 1
        assert got.intent_accuracy == 0.5

    def test_分层准确率(self):
        # Arrange：clear 全对、ambiguous 全错
        clear = dict(_MAP, item_id="intent-001", ambiguity_level="clear")
        amb = dict(_MAP, item_id="intent-041", ambiguity_level="ambiguous", expected_action="file_grep")
        verdicts = [
            self._v(clear, raw='{"action": "file_grep", "confidence": 0.9}'),
            self._v(amb, raw='{"action": "file_read", "confidence": 0.9}'),
        ]
        # Act
        got = aggregate(verdicts)
        # Assert
        assert got.intent_accuracy_clear == 1.0
        assert got.intent_accuracy_ambiguous == 0.0


class TestOntologyConstraintGain:
    """E2 核心产出口径：A1−A0 差值列；负值如实保留。"""

    def test_差值逐列计算(self):
        # Arrange：A0 acc 0.5 / A1 acc 0.75（其余同值）
        a0 = aggregate(
            [
                score_item(**_MAP, raw_output='{"action": "file_grep"}', confidence_floor=0.5),
                score_item(**_MAP, raw_output='{"action": "file_read"}', confidence_floor=0.5),
            ]
        )
        a1 = aggregate(
            [
                score_item(**_MAP, raw_output='{"action": "file_grep"}', confidence_floor=0.5),
                score_item(**_MAP, raw_output='{"action": "file_grep"}', confidence_floor=0.5),
            ]
        )
        # Act
        got = ontology_constraint_gain(a0, a1)
        # Assert
        assert got["diff"]["intent_accuracy"] == 0.5
        assert got["a0"]["intent_accuracy"] == 0.5
        assert got["a1"]["intent_accuracy"] == 1.0

    def test_负增益如实保留(self):
        # Arrange：A1 比 A0 差
        a0 = aggregate([score_item(**_MAP, raw_output='{"action": "file_grep"}', confidence_floor=0.5)])
        a1 = aggregate([score_item(**_MAP, raw_output='{"action": "file_read"}', confidence_floor=0.5)])
        # Act / Assert：负值不被吞掉（负结果同样是攻击性提问的答案）
        assert ontology_constraint_gain(a0, a1)["diff"]["intent_accuracy"] == -1.0
