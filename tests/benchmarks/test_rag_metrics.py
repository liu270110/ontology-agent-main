"""rag suite 指标口径单测（六维口径 = metrics.py 唯一事实源，本文件锁定其行为）。"""

from __future__ import annotations

import pytest

from benchmarks.suites.rag.metrics import (
    aggregate,
    estimate_tokens,
    faithfulness_rule,
    percentile_nearest_rank,
    recall_at_k,
    reciprocal_rank,
    score_query,
    sentence_support,
    split_sentences,
    token_counter,
)


class TestRecallAtK:
    """recall@k：top-k 命中相关文档数 / 相关总数。"""

    def test_recall_topk_部分命中按比例计(self):
        # Arrange：相关 2 文档，top5 命中其中 1 个
        retrieved = ["wo-001", "eq-002", "mx-003", "rg-004", "wo-005"]
        # Act
        value = recall_at_k(retrieved, {"wo-001", "wo-099"}, k=5)
        # Assert：命中 1/2
        assert value == pytest.approx(0.5)

    def test_recall_k_截断之外不计入(self):
        # Arrange：相关文档排在第 6 位（k=5 之外）
        retrieved = ["a", "b", "c", "d", "e", "wo-001"]
        # Act
        value = recall_at_k(retrieved, {"wo-001"}, k=5)
        # Assert：截断外不计数
        assert value == 0.0

    def test_recall_相关集空为零(self):
        # Arrange：空相关集
        # Act / Assert
        assert recall_at_k(["a"], set(), k=5) == 0.0


class TestReciprocalRank:
    """MRR 分子：首个相关文档名次的倒数。"""

    def test_首位命中得一分(self):
        # Arrange / Act / Assert
        assert reciprocal_rank(["wo-001", "b"], {"wo-001"}) == 1.0

    def test_第三位命中为三分之一(self):
        # Arrange / Act / Assert
        assert reciprocal_rank(["a", "b", "wo-001"], {"wo-001"}) == pytest.approx(1 / 3)

    def test_无命中为零(self):
        # Arrange / Act / Assert
        assert reciprocal_rank(["a", "b"], {"wo-001"}) == 0.0


class TestFaithfulnessRule:
    """规则版忠实度：答案句对引用集的字符 bigram 支撑率。"""

    def test_逐字引用答案满分(self):
        # Arrange：答案句逐字来自引文
        answer = "东港110kV变电站滨海甲线因雷击引起绝缘子闪络发生雷击跳闸。"
        citations = [answer]
        # Act
        value = faithfulness_rule(answer, citations, min_support=0.5)
        # Assert
        assert value == 1.0

    def test_编造答案零支撑为零(self):
        # Arrange：答案与引文无重叠
        answer = "这次事故完全是外星人破坏导致的。"
        citations = ["滨海甲线因雷击跳闸，影响320户。"]
        # Act
        value = faithfulness_rule(answer, citations, min_support=0.5)
        # Assert
        assert value == 0.0

    def test_半数句子有支撑得零点五(self):
        # Arrange：两句答案，一句逐字引用、一句编造
        quoted = "出线开关跳闸，滨海街道部分用户停电"
        answer = f"{quoted}。天气晴朗时线路运行平稳无需担心任何问题。"
        citations = [quoted]
        # Act
        value = faithfulness_rule(answer, citations, min_support=0.5)
        # Assert：1/2 句支撑
        assert value == pytest.approx(0.5)

    def test_无答案返回None与零区分(self):
        # Arrange：空答案
        # Act / Assert：None=未产出答案（no_answer），非零支撑
        assert faithfulness_rule(None, ["引用"]) is None
        assert faithfulness_rule("  ", ["引用"]) is None

    def test_支撑率按最大覆盖引用计(self):
        # Arrange：两个引用，第二个完整覆盖句子
        sentence = "危急缺陷处理时限不超过24小时"
        citations = ["与此无关的引文内容", f"{sentence}，严重缺陷不超过30天"]
        # Act
        value = sentence_support(sentence, citations)
        # Assert：第二个引用完全覆盖
        assert value == pytest.approx(1.0)


class TestSentenceSplit:
    """分句口径：。！？；;/换行，去空白保留长度≥2。"""

    def test_多标点切分(self):
        # Arrange
        text = "第一句。第二句！第三句？\n第四句；短"
        # Act
        parts = split_sentences(text)
        # Assert：「短」单字符被丢弃，其余按标点切分
        assert parts == ["第一句", "第二句", "第三句", "第四句"]

    def test_单字符句被丢弃(self):
        # Arrange
        # Act / Assert：「好」长度 1 被丢弃
        assert split_sentences("好。") == []


class TestPercentile:
    """nearest-rank 分位：ceil(p/100·n) 序位。"""

    def test_p50_奇数取中位(self):
        # Arrange / Act / Assert
        assert percentile_nearest_rank([3.0, 1.0, 2.0], 50) == 2.0

    def test_p95_向上取整序位(self):
        # Arrange：10 个值，p95 → ceil(9.5)=10 → 第 10 位
        values = [float(i) for i in range(1, 11)]
        # Act / Assert
        assert percentile_nearest_rank(values, 95) == 10.0

    def test_空集为零(self):
        # Arrange / Act / Assert
        assert percentile_nearest_rank([], 50) == 0.0


class TestTokenCounting:
    """token 计数：heuristic 口径 = CJK 字符×1 + ASCII 词×1。"""

    def test_中文按字符计(self):
        # Arrange：4 个汉字 + 1 个英文词 + 空白标点不计
        text = "滨海甲线 trip"
        # Act
        value = estimate_tokens(text)
        # Assert：4 字 + 1 词
        assert value == 5

    def test_空串为零(self):
        # Arrange / Act / Assert
        assert estimate_tokens("") == 0

    async def test_heuristic计数器工厂(self):
        # Arrange
        counter = token_counter("heuristic")
        # Act / Assert
        assert await counter.count("测试文本") == 4

    def test_未知后端拒绝(self):
        # Arrange / Act / Assert：口径突变必须显式失败，不静默降级
        with pytest.raises(ValueError, match="token_counter"):
            token_counter("gpt4")


class TestScoreQueryDocLevel:
    """文档级口径：chunk 级重复命中去重保序（首现位计名次）。"""

    def test_同文档多片段只计一次(self):
        # Arrange：检索返回 chunk 级（gold 文档重复出现）
        # Act
        score = score_query(
            query_id="q-001",
            retrieved_doc_ids=["wo-001", "wo-001", "mx-002", "wo-003"],
            relevant_doc_ids={"wo-001"},
            answer="逐字支撑的答案句",
            citation_texts=["逐字支撑的答案句"],
            latency_ms=5.0,
            cost_tokens=9,
            recall_k=3,
            min_support=0.5,
        )
        # Assert：去重后 3 文档、相关文档去重不重复计数（recall=1 而非 2）
        assert score.recall == 1.0
        assert score.reciprocal_rank == 1.0
        assert score.retrieved_doc_ids == ["wo-001", "mx-002", "wo-003"]


class TestAggregate:
    """宏平均聚合：faithfulness 只对有答案子集聚合并报告覆盖。"""

    def test_无答案查询计入no_answer(self):
        # Arrange：两查询一有答案一无答案
        s1 = score_query(
            query_id="q-001",
            retrieved_doc_ids=["wo-001"],
            relevant_doc_ids={"wo-001"},
            answer="逐字引用的答案",
            citation_texts=["逐字引用的答案"],
            latency_ms=10.0,
            cost_tokens=8,
            recall_k=5,
            min_support=0.5,
        )
        s2 = score_query(
            query_id="q-002",
            retrieved_doc_ids=[],
            relevant_doc_ids={"wo-002"},
            answer=None,
            citation_texts=[],
            latency_ms=30.0,
            cost_tokens=5,
            recall_k=5,
            min_support=0.5,
        )
        # Act
        agg = aggregate([s1, s2], recall_k=5)
        # Assert：recall 1.0+0.0 平均 0.5；faithfulness 只对 1 条计分；无答案 1 条
        assert agg.recall_at_k == pytest.approx(0.5)
        assert agg.mrr == pytest.approx(0.5)
        assert agg.faithfulness == 1.0
        assert agg.faithfulness_scored_queries == 1
        assert agg.no_answer_count == 1
        assert agg.latency_p50_ms == pytest.approx(10.0)  # nearest-rank：ceil(0.5·2)=1 → 排序第 1 位
        assert agg.cost_per_query_tokens == pytest.approx(6.5)
