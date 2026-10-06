"""rag v0 金标语料单测：三元组契约（数量/唯一性/引文逐字在正文/生成器与落盘零漂移）。"""

from __future__ import annotations

import pytest

from benchmarks.suites.rag.corpora.build_v0 import build_documents, build_queries, corpus_fingerprint, validate
from benchmarks.suites.rag.corpus import corpus_sha256, load_corpus, load_documents, load_queries


class TestCorpusV0:
    """v0 语料骨架契约：50 文档 × 20 查询。"""

    def test_语料数量与结构(self):
        # Arrange / Act
        docs, queries = load_corpus("v0")
        # Assert
        assert len(docs) == 50
        assert len(queries) == 20
        type_counts = {}
        for doc in docs:
            type_counts[doc.doc_type] = type_counts.get(doc.doc_type, 0) + 1
        assert type_counts == {"work_order": 20, "equipment": 15, "maintenance": 10, "regulation": 5}

    def test_id_唯一且查询金标可解析(self):
        # Arrange / Act
        docs, queries = load_corpus("v0")
        # Assert：doc_id/query_id 全唯一
        assert len({d.doc_id for d in docs}) == 50
        assert len({q.query_id for q in queries}) == 20
        # 每查询至少 1 条相关引用
        assert all(q.relevant for q in queries)

    def test_引文逐字在正文金标漂移防线(self):
        # Arrange
        docs, queries = load_corpus("v0")
        contents = {d.doc_id: d.content for d in docs}
        # Act / Assert：全部引用逐字包含
        for query in queries:
            for ref in query.relevant:
                assert ref.quote in contents[ref.doc_id], f"{query.query_id} 引文漂移：{ref.doc_id}"


class TestGeneratorConsistency:
    """生成器与入库 JSONL 零漂移：重跑 build_v0 逻辑应与落盘文件逐字节等价。"""

    def test_再生成与落盘一致(self):
        # Arrange：读入库文件
        docs_disk = load_documents("v0")
        queries_disk = load_queries("v0")
        # Act：内存再生成
        docs_gen = build_documents()
        queries_gen = build_queries()
        # Assert：文档逐条相等（生成器=落盘唯一来源）
        assert [
            {"doc_id": d.doc_id, "title": d.title, "content": d.content, "doc_type": d.doc_type} for d in docs_disk
        ] == docs_gen
        # 查询金标逐条相等
        assert [
            {
                "query_id": q.query_id,
                "query": q.query,
                "relevant": [{"doc_id": r.doc_id, "quote": r.quote, "note": r.note} for r in q.relevant],
            }
            for q in queries_disk
        ] == queries_gen

    def test_validate_拒绝坏金标(self):
        # Arrange：把首条查询的相关文档改指不存在的 doc_id（保持 20 条触发引用校验）
        docs = build_documents()
        queries = build_queries()
        queries[0]["relevant"][0]["doc_id"] = "wo-999"
        # Act / Assert
        with pytest.raises(ValueError, match="不存在"):
            validate(docs, queries)

    def test_指纹稳定(self):
        # Arrange / Act
        docs, queries = load_corpus("v0")
        once = corpus_sha256(docs, queries)
        again = corpus_fingerprint(
            [{"doc_id": d.doc_id, "title": d.title, "content": d.content, "doc_type": d.doc_type} for d in docs],
            [
                {
                    "query_id": q.query_id,
                    "query": q.query,
                    "relevant": [{"doc_id": r.doc_id, "quote": r.quote, "note": r.note} for r in q.relevant],
                }
                for q in queries
            ],
        )
        # Assert：两条指纹路径同值（跨运行可比性）
        assert once == again


class TestLoaderGuards:
    """loader 防线：缺文件/坏行显式报错（评测不可带病起跑）。"""

    def test_缺版本显式报错(self):
        # Arrange / Act / Assert
        with pytest.raises(FileNotFoundError):
            load_documents("v999")
