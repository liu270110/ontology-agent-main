"""rag suite 语料装载与校验（金标三元组契约的唯一读入口）。

金标 JSONL 由 corpora/build_v0.py 生成并版本化入库；本模块只读不改：
- load_documents(version) → list[CorpusDocument]（50 文档）；
- load_queries(version) → list[GoldQuery]（20 查询，relevant 含逐字引文）；
- 双向断言：quote ∈ 对应文档 content（金标漂移即抛错，评测不可带病起跑）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

_CORPORA_ROOT = Path(__file__).resolve().parent / "corpora"
_DOC_COUNT = 50
_QUERY_COUNT = 20


@dataclass(slots=True, frozen=True)
class CorpusDocument:
    """标准语料文档（title 进 kb 上传 title；content 全文进检索面）。"""

    doc_id: str
    title: str
    content: str
    doc_type: str


@dataclass(slots=True, frozen=True)
class GoldReference:
    """单条相关引用：相关文档 + 逐字相关片段（引文在文档正文内）。"""

    doc_id: str
    quote: str
    note: str = ""


@dataclass(slots=True, frozen=True)
class GoldQuery:
    """金标查询：查询文本 + 相关文档集合（doc 级 recall/MRR 口径；quote 留作片段级扩展）。"""

    query_id: str
    query: str
    relevant: tuple[GoldReference, ...]


def load_documents(version: str = "v0") -> list[CorpusDocument]:
    """读语料文档 JSONL 并校验（数量/唯一性）。"""
    path = _CORPORA_ROOT / version / "documents.jsonl"
    rows = _read_jsonl(path)
    docs = [
        CorpusDocument(
            doc_id=str(r["doc_id"]), title=str(r["title"]), content=str(r["content"]), doc_type=str(r["doc_type"])
        )
        for r in rows
    ]
    if len(docs) != _DOC_COUNT:
        raise ValueError(f"{path} 文档数应为 {_DOC_COUNT}，实际 {len(docs)}")
    if len({d.doc_id for d in docs}) != len(docs):
        raise ValueError(f"{path} doc_id 重复")
    return docs


def load_queries(version: str = "v0") -> list[GoldQuery]:
    """读金标查询 JSONL 并校验（数量/唯一性/非空相关集）。"""
    path = _CORPORA_ROOT / version / "queries.jsonl"
    rows = _read_jsonl(path)
    queries = [
        GoldQuery(
            query_id=str(r["query_id"]),
            query=str(r["query"]),
            relevant=tuple(
                GoldReference(doc_id=str(x["doc_id"]), quote=str(x["quote"]), note=str(x.get("note", "")))
                for x in r["relevant"]
            ),
        )
        for r in rows
    ]
    if len(queries) != _QUERY_COUNT:
        raise ValueError(f"{path} 查询数应为 {_QUERY_COUNT}，实际 {len(queries)}")
    if len({q.query_id for q in queries}) != len(queries):
        raise ValueError(f"{path} query_id 重复")
    for q in queries:
        if not q.relevant:
            raise ValueError(f"{q.query_id} 无相关文档")
    return queries


def load_corpus(version: str = "v0") -> tuple[list[CorpusDocument], list[GoldQuery]]:
    """成对装载并做金标交叉校验（引文逐字在正文；doc_id 可解析）。"""
    docs = load_documents(version)
    queries = load_queries(version)
    contents = {d.doc_id: d.content for d in docs}
    for q in queries:
        for ref in q.relevant:
            content = contents.get(ref.doc_id)
            if content is None:
                raise ValueError(f"{q.query_id} 引用了不存在的文档 {ref.doc_id}")
            if ref.quote not in content:
                raise ValueError(f"{q.query_id} 引文不在 {ref.doc_id} 正文（金标漂移）：{ref.quote[:40]}…")
    return docs, queries


def corpus_sha256(docs: list[CorpusDocument], queries: list[GoldQuery]) -> str:
    """语料指纹（与 build_v0.corpus_fingerprint 同构；随结果 JSON 落盘供跨运行比对）。"""
    payload = json.dumps(
        {
            "documents": [
                {"doc_id": d.doc_id, "title": d.title, "content": d.content, "doc_type": d.doc_type} for d in docs
            ],
            "queries": [
                {
                    "query_id": q.query_id,
                    "query": q.query,
                    "relevant": [{"doc_id": r.doc_id, "quote": r.quote, "note": r.note} for r in q.relevant],
                }
                for q in queries
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"语料缺失：{path}（先运行 corpora/build_v0.py 生成或检查版本号）")
    rows: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path} 第 {lineno} 行不是 JSON 对象")
        rows.append(row)
    return rows
