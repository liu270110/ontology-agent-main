"""naive RAG 基线单测：统一 retrieve 接口 / BM25 排序正确性 / 抽取式读者口径。

向量基线的活体链路（真嵌入端点）按 ollama 标记口径做探活 skip——端点不在时跳过不红。
"""

from __future__ import annotations

import pytest

from benchmarks.suites.rag.competitors.naive_rag import (
    NaiveBm25Retriever,
    NaiveVectorRetriever,
    RetrievedDoc,
    compose_extractive_answer,
    tokenize_cn,
)


def _docs() -> list[RetrievedDoc]:
    """小型确定性语料（覆盖工单/设备两种文本形态）。"""
    return [
        RetrievedDoc(
            "wo-001",
            "停电工单 GD-2601-001（滨海甲线）",
            "滨海甲线因雷击引起绝缘子闪络发生雷击跳闸，影响320户。抢修班已更换受损绝缘子。",
            0.0,
        ),
        RetrievedDoc(
            "wo-002",
            "停电工单 GD-2601-002（翠竹路）",
            "市政施工挖断电缆造成外力破坏停电，影响64户。抢修班重新敷设电缆。",
            0.0,
        ),
        RetrievedDoc(
            "eq-001",
            "设备档案 SB-1002（东港110kV变电站2号主变）",
            "设备编号：SB-1002。额定参数：50 MVA。投运日期：2018-09-12。最近巡视结论：油温略高，列入跟踪。",
            0.0,
        ),
    ]


class TestTokenizer:
    """朴素分词口径：CJK 字符二元组 + ASCII 词元。"""

    def test_cjk_切成二元组(self):
        # Arrange / Act / Assert：滨海甲线 → 滨海/海甲/甲线
        assert tokenize_cn("滨海甲线") == ["滨海", "海甲", "甲线"]

    def test_单字保留_ascii_按词(self):
        # Arrange / Act / Assert
        assert tokenize_cn("线 LGJ240") == ["线", "lgj240"]

    def test_标点空白不产词(self):
        # Arrange / Act / Assert
        assert tokenize_cn("，。！结束") == ["结束"]


class TestNaiveBm25:
    """全文 BM25 基线：相关性排序与接口形状。"""

    async def test_独占词文档排首位(self):
        # Arrange
        retriever = NaiveBm25Retriever(_docs(), k1=1.5, b=0.75)
        # Act：query 只与 wo-001 共享「雷击」词面
        hits = await retriever.retrieve("雷击跳闸原因", k=3)
        # Assert：独占词面文档居首；零分文档不进命中集（本语料仅 wo-001 与查询共词）
        assert hits[0].doc_id == "wo-001"
        assert len(hits) == 1

    async def test_k_截断生效(self):
        # Arrange
        retriever = NaiveBm25Retriever(_docs())
        # Act / Assert
        assert len(await retriever.retrieve("雷击", k=1)) == 1

    async def test_零重叠返回空(self):
        # Arrange
        retriever = NaiveBm25Retriever(_docs())
        # Act / Assert：完全无共享词面 → 零分文档不返回
        assert await retriever.retrieve("量子纠缠态", k=3) == []

    def test_空文档集拒绝(self):
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="非空"):
            NaiveBm25Retriever([])


class TestComposeAnswer:
    """naive 抽取式读者：前 N 句拼装（口径对齐 ours 句级拼装）。"""

    def test_前两句拼装(self):
        # Arrange
        content = "第一句内容。第二句内容。第三句内容。"
        # Act
        answer = compose_extractive_answer(content, max_sentences=2)
        # Assert
        assert answer == "第一句内容第二句内容"

    def test_空内容返回None(self):
        # Arrange / Act / Assert
        assert compose_extractive_answer("。。", max_sentences=3) is None


class TestNaiveVector:
    """简单向量基线：真嵌入端点活体链路（TEI 不在则 skip）。"""

    async def test_余弦排序首位为语义最近文档(self):
        # Arrange：探活 TEI（不在即 skip，不红）
        import httpx

        try:
            probe = httpx.post("http://127.0.0.1:18002/embed", json={"inputs": ["探活"]}, timeout=3.0)
        except httpx.HTTPError:
            pytest.skip("TEI 嵌入端点不可达，跳过向量基线活体用例")
        assert probe.status_code == 200
        retriever = NaiveVectorRetriever(_docs(), embed_base_url="http://127.0.0.1:18002", embed_protocol="tei")
        await retriever.ensure_indexed()
        # Act：query 与主变档案语义最近
        hits = await retriever.retrieve("主变额定容量是多少", k=3)
        # Assert
        assert hits[0].doc_id == "eq-001"

    async def test_未索引先检索显式报错(self):
        # Arrange
        retriever = NaiveVectorRetriever(_docs(), embed_base_url="http://127.0.0.1:18002")
        # Act / Assert
        with pytest.raises(RuntimeError, match="未索引"):
            await retriever.retrieve("任意", k=3)
