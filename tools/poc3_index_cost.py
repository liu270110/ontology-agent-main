#!/usr/bin/env python3
"""PoC3 索引成本对比 harness（实施计划 13 篇 2.4；平台检索实现参考 services/kb/retrieval/）。

同一自造电力语料（确定性生成，150 篇 / 目标 50~200 chunks）上对比两路线：

- LazyGraphRAG 基线（平台默认档）：chunk + 嵌入 + BM25 索引，查询时 RRF 融合 + 图扩展；
  实测索引期 token/耗时/存储与查询期延迟（离线复刻 lite 管线，零 Neo4j、零预建社区）。
- 完整 GraphRAG 预估：chunk 级实体抽取（LLM 逐 chunk）+ 层次化社区检测
  （networkx louvain 离线跑真实共现图，近似 Leiden——networkx 无 Leiden 实现，见输出标注）
  + 逐社区报告（LLM）。社区报告 token 按真实 prompt 模板估算；LLM 可用时实测
  5 个社区样本 + 3 个 chunk 抽取样本，按 token 规模外推全量。

口径声明：
- token 估算 = len(text)//2（中文 est 口径，随前批 PoC③ 冻结）；LLM 实测样本以端点 usage 为准；
- 嵌入实测走 Ollama bge-m3（平台 services/kb/retrieval/embed.py 同源模型）；不可用时按
  前批 PoC③ 实测吞吐 937.8 est-tokens/s 外推并标注【外推】；
- LLM 实测样本经预热调用后采集（模型加载/首 token 抖动不计入样本）。

用法：
  python tools/poc3_index_cost.py                # 全量（嵌入+LLM 样本实测）
  python tools/poc3_index_cost.py --no-llm       # 跳过 LLM 样本（全部走模板估算）
  python tools/poc3_index_cost.py --no-embed     # 跳过嵌入实测（按基线吞吐外推）

结果写 stdout（markdown 矩阵）与 tools/poc3_results.json。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SEED: Final = 42
N_DOCS: Final = 150  # 每篇确定性生成约 1 chunk（383~470 字），共 150 chunks（任务目标 50~200 档内）
CHUNK_TARGET_CHARS: Final = 500  # 对齐平台 chunking 目标 512±128 token 的字符级近似
CHUNK_MAX_CHARS: Final = 680  # 超限才断句（≈1.35x 目标）
RRF_K: Final = 60  # 对齐 services/kb/retrieval/retrieve.py RRF_K
RECALL_POOL: Final = 50  # 对齐同模块 RECALL_POOL
GRAPH_EXPAND_CAP: Final = 100  # 对齐同模块 GRAPH_CANDIDATE_CAP
EMBED_BATCH: Final = 32
EMBED_DIM: Final = 1024  # bge-m3
EMBED_BASELINE_TOK_S: Final = 937.8  # 前批 PoC③ 实测（bge-m3, Ollama）；仅 --no-embed 降级时引用
N_EXTRACT_SAMPLES: Final = 3  # chunk 级实体抽取实测样本数
N_REPORT_SAMPLES: Final = 5  # 社区报告实测样本数
N_QUERIES: Final = 6
LLM_TIMEOUT_S: Final = 150.0
PROBE_TIMEOUT_S: Final = 5.0
LLM_MAX_TOKENS: Final = 600  # 抽取/报告 JSON 足够；压低上限避免复述循环拖满长生成
COMMUNITY_L1_MIN_SIZE: Final = 8  # 规模达标的 L0 社区才做第二层切分
EXTRACT_OUT_EST_TOKENS: Final = 90  # 单 chunk 抽取输出 est-token 假设（实测缺失时的外推参数）
REPORT_OUT_EST_TOKENS: Final = 120  # 单社区报告输出 est-token 假设（实测缺失时的外推参数）
DEFAULT_OUTPUT: Final = Path(__file__).resolve().parent / "poc3_results.json"
MB: Final = 1024 * 1024

EXTRACT_TEMPLATE: Final = """你是电力知识图谱构建引擎。从下面的文本中抽取实体（设备/区段/馈线/人员/工单/用户）与关系，\
只输出 JSON：{{"entities": [{{"name": "...", "type": "..."}}], "relations": [{{"subject": "...", \
"predicate": "...", "object": "..."}}]}}。只抽文本明确陈述的内容。

文本：
{text}"""

REPORT_TEMPLATE: Final = """你是知识图谱分析员。以下是一个实体社区（图谱中联系紧密的实体簇）的成员与内部关联摘要，\
请生成结构化社区报告。只输出 JSON：{{"title": "...", "summary": "60~100字摘要", \
"findings": ["要点1", "要点2"]}}。

社区实体（{n_entities}个）：{entities}
社区内部共现（{n_relations}条）：{relations}"""

_SYSTEM_EXTRACT: Final = "你是电力领域信息抽取引擎，只输出 JSON。"
_SYSTEM_REPORT: Final = "你是知识图谱分析员，只输出 JSON。"


# ---------------------------------------------------------------------------
# 确定性电力语料生成器
# ---------------------------------------------------------------------------

FEEDERS: Final[tuple[str, ...]] = ("滨河线", "朝阳线", "东环线", "西城线", "南郊线", "开发区线")
SECTIONS: Final[tuple[str, ...]] = tuple(f"{feeder}{seg}" for feeder in FEEDERS for seg in ("东段", "西段", "工业区段"))
CREWS: Final[tuple[str, ...]] = ("抢修一班", "抢修二班", "抢修三班")
LEADS: Final[tuple[str, ...]] = ("张建国", "李卫东", "王志强", "赵国庆", "刘长河", "陈永刚")
CUSTOMERS: Final[tuple[str, ...]] = ("人民医院", "实验中学", "纺织厂", "钢铁厂", "水泥厂", "机械厂", "化工厂", "啤酒厂")
STATUSES: Final[tuple[str, ...]] = ("正常", "受控", "故障停运", "检修")

# 实体词典（宽松口径的图构建与查询都用它做子串识别；词长降序匹配避免前缀截断）
_ENTITY_VOCAB: Final[tuple[str, ...]] = tuple(
    sorted((*FEEDERS, *SECTIONS, *CREWS, *LEADS, *CUSTOMERS), key=len, reverse=True)
)


def _switch_of(feeder_index: int, k: int) -> str:
    return f"{FEEDERS[feeder_index]}{100 + 7 * k}开关"


def _transformer_of(i: int) -> str:
    return f"城东{i}号配变" if i % 2 == 0 else f"开发区{i}号配变"


def _order_of(i: int) -> str:
    return f"GD-2026-0{301 + i:03d}"


def _tail_paragraph(rng: random.Random, i: int, feeder: str, section: str, crew: str, lead: str, customer: str) -> str:
    """追加两段（影响面/历史/后续安排 + 处置时间线），使文档落在真实 chunk 尺寸量级。"""
    other = SECTIONS[(FEEDERS.index(feeder) * 3 + rng.randrange(3))]
    feeder2 = FEEDERS[(FEEDERS.index(feeder) + 2) % len(FEEDERS)]
    parts = [
        f"影响评估：{section}共涉及{CUSTOMERS[rng.randrange(len(CUSTOMERS))]}、{customer}"
        f"等{rng.randrange(3, 12)}个用户，由{feeder}经{_switch_of(FEEDERS.index(feeder), rng.randrange(1, 20))}供电。",
        f"历史记录：{section}上年共停电{rng.randrange(1, 6)}次，最近一次为{_order_of((i + 31) % 60)}，"
        f"处置班组为{CREWS[rng.randrange(len(CREWS))]}。",
        f"后续安排：{crew}（负责人{lead}）明日对{other}开展特巡，重点检查柱上开关与配变接头温度，结果报调度备案。",
        f"风险提示：若{feeder}再次故障，{customer}将转入{feeder2}转供，转供操作预计{rng.randrange(10, 40)}分钟完成。",
    ]
    rng.shuffle(parts)
    h2 = (rng.randrange(6, 22) + 1) % 24
    m2 = rng.choice(("10", "15", "25", "35", "45"))
    timeline = (
        f"处置时间线：{rng.randrange(5, 25)}分钟内{crew}完成到场签到，"
        f"现场检查确认{section}一处故障点并隔离；{h2}时{m2}分具备送电条件，"
        f"调度确认{section}恢复供电后{crew}撤离，工单资料{rng.randrange(1, 3)}日内归档，"
        f"本次处置共出动抢修人员{rng.randrange(2, 7)}人、车辆{rng.randrange(1, 3)}台。"
    )
    return "".join(parts) + timeline


def generate_docs(seed: int = SEED) -> list[dict[str, str]]:
    """生成 150 篇确定性电力文档（doc_id + text，每篇两段，383~470 字）。"""
    rng = random.Random(seed)
    doc_types: Final = ("fault_report", "dispatch_log", "ticket", "ledger", "analysis")
    docs: list[dict[str, str]] = []
    for i in range(N_DOCS):
        dtype = doc_types[i % len(doc_types)]
        f = rng.randrange(len(FEEDERS))
        feeder = FEEDERS[f]
        section = SECTIONS[f * 3 + rng.randrange(3)]
        switch = _switch_of(f, rng.randrange(1, 20))
        crew = CREWS[rng.randrange(len(CREWS))]
        lead = LEADS[rng.randrange(len(LEADS))]
        customer = CUSTOMERS[rng.randrange(len(CUSTOMERS))]
        order = _order_of(i)
        day = rng.randrange(1, 29)
        hour = rng.randrange(6, 22)
        minute = rng.choice(("05", "10", "20", "30", "40", "50"))
        status = STATUSES[rng.randrange(len(STATUSES))]
        if dtype == "fault_report":
            text = (
                f"{day}日{hour}时{minute}分，10kV{feeder}{switch}保护动作跳闸，{feeder}全线失压。"
                f"巡线确认故障点位于{section}，{section}停电。{feeder}转入故障停运，"
                f"故障工单{order}已生成。{crew}（负责人{lead}）被派往{section}处置故障，"
                f"区段内{customer}供电受影响。"
            )
        elif dtype == "dispatch_log":
            text = (
                f"调度日志 {day}日：{feeder}过负荷告警，{feeder}状态转为受控。"
                f"{customer}负荷由{feeder}切换至{FEEDERS[(f + 1) % len(FEEDERS)]}供电。"
                f"{section}保持{status}，{switch}遥测正常。{hour}时{minute}分确认操作完成。"
            )
        elif dtype == "ticket":
            text = (
                f"操作票 P-2026-0{i:03d}：任务为{feeder}{switch}由运行转检修。"
                f"操作前{switch}状态为运行，涉及{section}，操作班组{crew}（负责人{lead}）。"
                f"操作完成后{section}转入{status}。"
            )
        elif dtype == "ledger":
            tid = rng.randrange(1, 9)
            transformer = _transformer_of(tid)
            text = (
                f"设备台账：{transformer}接于{section}，状态{status}，供电用户{customer}。"
                f"{transformer}由{feeder}{switch}供电，{day}日台账更新。"
                f"该区段抢修责任班组{crew}，负责人{lead}。"
            )
        else:
            text = (
                f"月度分析：{feeder}本月跳闸{rng.randrange(1, 4)}次，均位于{section}。"
                f"{feeder}当前状态{status}，{customer}由{section}供电。"
                f"故障工单{order}平均处置时长{rng.randrange(40, 120)}分钟，"
                f"{crew}（负责人{lead}）处置效率达标。"
            )
        text = text + _tail_paragraph(rng, i, feeder, section, crew, lead, customer)
        docs.append({"doc_id": f"{dtype}-{i:03d}", "text": text})
    return docs


def chunk_document(
    doc_id: str, text: str, target: int = CHUNK_TARGET_CHARS, max_chars: int = CHUNK_MAX_CHARS
) -> list[str]:
    """段落聚合式分块：按句号切段，聚到 target 字符，超 max_chars 才强制截断。"""
    sentences = [s for s in re.split(r"(?<=[。；])", text) if s]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for sentence in sentences:
        if buf and size + len(sentence) > max_chars:
            chunks.append("".join(buf))
            buf, size = [], 0
        buf.append(sentence)
        size += len(sentence)
        if size >= target:
            chunks.append("".join(buf))
            buf, size = [], 0
    if buf:
        chunks.append("".join(buf))
    return [f"{chunk}\n[doc:{doc_id}]" for chunk in chunks]


def est_tokens(text: str) -> int:
    """中文 est 口径 token 估算（= len//2，随前批 PoC③ 冻结）。"""
    return max(1, len(text) // 2)


def extract_entities(text: str) -> frozenset[str]:
    """词典子串识别（词长降序，命中即消耗，避免前缀实体重复命中）。"""
    found: list[str] = []
    rest = text
    for term in _ENTITY_VOCAB:
        if term in rest:
            found.append(term)
            rest = rest.replace(term, "〓")
    # 开关/配变/工单等带编号实体用模式补齐
    found += re.findall(r"[0-9A-Za-z]*\d{2,4}开关|\S?\d号配变|GD-\d{4}-\d{3,4}", text)
    return frozenset(found)


# ---------------------------------------------------------------------------
# BM25（纯 Python，bigram 分词）与查询管线
# ---------------------------------------------------------------------------

_TOKEN_RE: Final = re.compile(r"[0-9a-z]+|[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """ASCII 词 + 中文二元组。"""
    tokens: list[str] = []
    for run in _TOKEN_RE.findall(text.lower()):
        if run.isascii():
            tokens.append(run)
        elif len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


class Bm25Index:
    """轻量 BM25（k1=1.5, b=0.75），只服务本 harness 的成本/延迟测量。"""

    def __init__(self) -> None:
        self.k1 = 1.5
        self.b = 0.75
        self.postings: dict[str, list[tuple[int, int]]] = {}
        self.doc_len: list[int] = []
        self.avgdl = 0.0

    def build(self, tokenized: list[list[str]]) -> None:
        counts: dict[str, Counter[int]] = defaultdict(Counter)
        self.doc_len = [len(tokens) for tokens in tokenized]
        for cid, tokens in enumerate(tokenized):
            for term, tf in Counter(tokens).items():
                counts[term][cid] = tf
        self.postings = {term: sorted(counter.items()) for term, counter in counts.items()}
        self.avgdl = sum(self.doc_len) / max(1, len(self.doc_len))

    def storage_bytes(self) -> int:
        """倒排索引序列化字节估算（term + postings 数组）。"""
        return sum(len(term.encode("utf-8")) + 8 + 12 * len(plist) for term, plist in self.postings.items())

    def search(self, query_tokens: list[str], top_k: int) -> list[tuple[int, float]]:
        n = len(self.doc_len)
        scores: dict[int, float] = defaultdict(float)
        for term in query_tokens:
            plist = self.postings.get(term)
            if not plist:
                continue
            idf = math.log(1 + (n - len(plist) + 0.5) / (len(plist) + 0.5))
            for cid, tf in plist:
                denom = tf + self.k1 * (1 - self.b + self.b * self.doc_len[cid] / self.avgdl)
                scores[cid] += idf * tf * (self.k1 + 1) / denom
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return ranked


def rrf_fuse(rankings: list[list[int]], top_k: int) -> list[tuple[int, float]]:
    """RRF 融合（k=60，对齐平台 retrieve.py）。"""
    scores: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            scores[cid] += 1.0 / (RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]


# ---------------------------------------------------------------------------
# 嵌入（Ollama bge-m3，平台 embed.py 同源模型）与 LLM 客户端
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EmbedStats:
    """嵌入阶段统计（measured=False 表示按基线吞吐外推）。"""

    measured: bool
    seconds: float
    tokens: int
    storage_bytes: int

    @property
    def tok_per_s(self) -> float:
        return self.tokens / self.seconds if self.seconds else 0.0


class OllamaEmbedder:
    def __init__(self, url: str, model: str = "bge-m3") -> None:
        self._client = httpx.Client(timeout=600.0)
        self._url = url.rstrip("/")
        self.model = model

    def probe(self) -> bool:
        try:
            resp = self._client.get(f"{self._url}/api/tags", timeout=PROBE_TIMEOUT_S)
            if resp.status_code != 200:
                return False
            models = resp.json().get("models", [])
            return any(str(m.get("name", "")).startswith(self.model) for m in models)
        except (httpx.HTTPError, ValueError):
            return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.post(f"{self._url}/api/embed", json={"model": self.model, "input": texts})
        resp.raise_for_status()
        return resp.json()["embeddings"]

    def close(self) -> None:
        self._client.close()


class ChatClient:
    """最小 OpenAI 兼容 chat 客户端（探测 + JSON 调用，记录 usage/耗时）。"""

    def __init__(self, base_url: str, model: str, api_key: str | None) -> None:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=LLM_TIMEOUT_S)
        self.model = model
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.latencies: list[float] = []

    def probe(self) -> bool:
        try:
            return self._client.get("/models", timeout=PROBE_TIMEOUT_S).status_code == 200
        except httpx.HTTPError:
            return False

    def chat_json(self, system: str, user: str) -> tuple[str, str]:
        """返回 (content, error)；失败重试一次。"""
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": LLM_MAX_TOKENS,
            "temperature": 0,
            "repetition_penalty": 1.05,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        err = ""
        for _attempt in range(2):
            started = time.perf_counter()
            try:
                resp = self._client.post("/chat/completions", json=payload)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                err = f"http_error: {exc}"
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
                err = "no_choices"
                continue
            message: dict[str, object] = choices[0].get("message") or {}  # type: ignore[union-attr]
            return str(message.get("content") or ""), ""
        return "", err

    def close(self) -> None:
        self._client.close()


@dataclass(slots=True)
class LlmUnitCost:
    """由实测样本外推的单次调用成本模型（est token 计）。"""

    measured: bool
    n_samples: int
    median_prompt_tokens: float
    median_completion_tokens: float
    median_latency_s: float

    def scale(self, est_input_tokens: int, est_output_tokens: int) -> tuple[float, int]:
        """按 token 规模外推 (耗时秒, 总 token)。"""
        in_toks = self.median_prompt_tokens or 1.0
        out_toks = self.median_completion_tokens or 1.0
        ratio = (est_input_tokens / in_toks) * 0.3 + (est_output_tokens / out_toks) * 0.7
        latency = self.median_latency_s * max(ratio, 0.1)
        return latency, est_input_tokens + est_output_tokens


# ---------------------------------------------------------------------------
# 社区检测（networkx louvain 层次化；近似 Leiden，输出处标注）
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Community:
    """一个社区及其报告 prompt 估算材料。"""

    level: int
    members: list[str]
    digest: str  # 实体清单 + 共现摘要（真实可渲染进 prompt 模板）
    est_prompt_tokens: int
    est_output_tokens: int = 120  # 报告 JSON 约 240 字 ≈ 120 est-token


def build_cooccurrence_graph(chunks: list[ChunkData]) -> tuple[dict[str, dict[str, int]], dict[str, list[int]]]:
    """实体共现图：边权=共享 chunk 数；返回 (graph, entity→chunks)。"""
    entity_chunks: dict[str, list[int]] = defaultdict(list)
    for chunk in chunks:
        for entity in chunk.entities:
            entity_chunks[entity].append(chunk.chunk_id)
    graph: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for chunk in chunks:
        ents = sorted(chunk.entities)
        for i, a in enumerate(ents):
            for b in ents[i + 1 :]:
                graph[a][b] += 1
                graph[b][a] += 1
    return graph, entity_chunks


def detect_communities(graph: dict[str, dict[str, int]]) -> tuple[list[Community], int]:
    """层次化 louvain：L0 全图 → 对达标社区在诱导子图上再切 L1；返回 (社区列表, L0 顶层分区数)。"""
    import networkx as nx

    nx_graph = nx.Graph()
    for a, nbrs in graph.items():
        for b, w in nbrs.items():
            nx_graph.add_edge(a, b, weight=w)
    l0_all: list[set[str]] = []
    if nx_graph.number_of_nodes():
        l0_all = [set(c) for c in nx.community.louvain_communities(nx_graph, seed=SEED, resolution=1.0)]
    communities: list[Community] = []
    n_top_cells = len(l0_all)  # L0 顶层分区数（达标者被二次切分为 L1，计数仍保留）
    for members in sorted(l0_all, key=len, reverse=True):
        if len(members) < COMMUNITY_L1_MIN_SIZE:
            communities.append(_make_community(0, sorted(members), graph))
            continue
        sub = nx_graph.subgraph(members)
        try:
            sub_parts = [set(c) for c in nx.community.louvain_communities(sub, seed=SEED, resolution=1.2)]
        except Exception:  # noqa: BLE001 — 单社区切分失败不阻塞（退化为单 L0 社区）
            sub_parts = [members]
        if len(sub_parts) <= 1:
            communities.append(_make_community(0, sorted(members), graph))
            continue
        for part in sub_parts:
            communities.append(_make_community(1, sorted(part), graph))
    return communities, n_top_cells


def _make_community(level: int, members: list[str], graph: dict[str, dict[str, int]]) -> Community:
    """由真实成员与共现边构造社区 digest 与报告 prompt token 估算。"""
    top_pairs: list[tuple[int, str, str]] = []
    for a in members:
        for b, w in graph.get(a, {}).items():
            if b in members and a < b:
                top_pairs.append((w, a, b))
    top_pairs.sort(reverse=True)
    relations = sum(w for w, _a, _b in top_pairs)
    rel_parts = [f"{a}—{b}(共现{w})" for w, a, b in top_pairs[:8]]
    digest = f"实体：{'、'.join(members[:40])}；关联：{'、'.join(rel_parts) or '稀疏'}"
    prompt = REPORT_TEMPLATE.format(
        n_entities=len(members),
        entities="、".join(members[:40]),
        n_relations=relations,
        relations="；".join(rel_parts) or "无",
    )
    return Community(level=level, members=members, digest=digest, est_prompt_tokens=est_tokens(prompt))


@dataclass(slots=True)
class ChunkData:
    """chunk 及其派生数据。"""

    chunk_id: int
    doc_id: str
    text: str
    tokens: int
    entities: frozenset[str] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def pick_median_samples(items: list, n: int, key) -> list:
    """按 key 排序取中位附近的 n 个样本（确定性）。"""
    if len(items) <= n:
        return list(items)
    ordered = sorted(items, key=key)
    start = (len(ordered) - n) // 2
    return ordered[start : start + n]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="PoC3 索引成本对比 harness（LazyGraphRAG vs 完整 GraphRAG）")
    parser.add_argument("--no-llm", action="store_true", help="跳过 LLM 实测样本（全部走模板估算）")
    parser.add_argument("--no-embed", action="store_true", help="跳过嵌入实测（按基线吞吐外推）")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="JSON 结果输出路径")
    args = parser.parse_args(argv)

    result: dict[str, object] = {
        "meta": {
            "generated_at": datetime.now(UTC).isoformat(),
            "seed": SEED,
            "token_rule": "est 口径 = len(text)//2（中文，随前批 PoC③ 冻结）；LLM 样本以端点 usage 为准",
            "community_algo": "networkx louvain（层次化，近似 Leiden——networkx 无 Leiden 实现）",
        }
    }

    # ── 1. 语料 + 分块 + BM25 索引（LazyGraphRAG 基线静态部分）───────────────
    docs = generate_docs()
    t0 = time.perf_counter()
    raw_chunks: list[tuple[str, str]] = []
    for doc in docs:
        for chunk_text in chunk_document(doc["doc_id"], doc["text"]):
            raw_chunks.append((doc["doc_id"], chunk_text))
    chunks = [
        ChunkData(
            chunk_id=i,
            doc_id=doc_id,
            text=text,
            tokens=est_tokens(text),
            entities=extract_entities(text),
        )
        for i, (doc_id, text) in enumerate(raw_chunks)
    ]
    chunk_seconds = time.perf_counter() - t0
    corpus_tokens = sum(c.tokens for c in chunks)

    t0 = time.perf_counter()
    bm25 = Bm25Index()
    bm25.build([tokenize(c.text) for c in chunks])
    bm25_seconds = time.perf_counter() - t0
    bm25_bytes = bm25.storage_bytes()
    print(
        f"[语料] {len(docs)} 篇 → {len(chunks)} chunks | {corpus_tokens:,} est-tokens | "
        f"分块 {chunk_seconds:.3f}s | BM25 索引 {bm25_seconds:.3f}s（存储 {bm25_bytes / MB:.2f}MB）",
        flush=True,
    )
    result["corpus"] = {
        "n_docs": len(docs),
        "n_chunks": len(chunks),
        "corpus_est_tokens": corpus_tokens,
        "chunk_target_chars": CHUNK_TARGET_CHARS,
        "chunk_median_chars": int(statistics.median(len(c.text) for c in chunks)),
        "chunking_seconds": round(chunk_seconds, 4),
        "bm25_index_seconds": round(bm25_seconds, 4),
        "bm25_storage_bytes": bm25_bytes,
        "n_entities": len({e for c in chunks for e in c.entities}),
    }

    # ── 2. 嵌入（LazyGraphRAG 基线）───────────────────────────────────────
    embed_stats = EmbedStats(measured=False, seconds=0.0, tokens=corpus_tokens, storage_bytes=0)
    embedder: OllamaEmbedder | None = None
    chunk_vectors: list[list[float]] | None = None
    if not args.no_embed:
        embedder = OllamaEmbedder("http://127.0.0.1:11434")
        if embedder.probe():
            try:
                embedder.embed(["预热"])  # 模型加载不计入测量
                t0 = time.perf_counter()
                chunk_vectors = []
                for i in range(0, len(chunks), EMBED_BATCH):
                    batch = [c.text for c in chunks[i : i + EMBED_BATCH]]
                    chunk_vectors.extend(embedder.embed(batch))
                embed_stats = EmbedStats(
                    measured=True,
                    seconds=time.perf_counter() - t0,
                    tokens=corpus_tokens,
                    storage_bytes=len(chunks) * EMBED_DIM * 4,
                )
            except httpx.HTTPError as exc:
                print(f"[降级] 嵌入失败，按基线吞吐外推：{exc}", flush=True)
        else:
            print("[降级] Ollama/bge-m3 不可用，按基线吞吐外推", flush=True)
        embedder.close()
    if not embed_stats.measured:
        embed_stats = EmbedStats(
            measured=False,
            seconds=corpus_tokens / EMBED_BASELINE_TOK_S,
            tokens=corpus_tokens,
            storage_bytes=len(chunks) * EMBED_DIM * 4,
        )
    print(
        f"[嵌入] measured={embed_stats.measured} | {embed_stats.seconds:.2f}s | "
        f"{embed_stats.tok_per_s:.1f} tok/s | 向量存储 {embed_stats.storage_bytes / MB:.2f}MB",
        flush=True,
    )
    result["embed"] = {
        "measured": embed_stats.measured,
        "seconds": round(embed_stats.seconds, 3),
        "tokens": embed_stats.tokens,
        "tok_per_s": round(embed_stats.tok_per_s, 1),
        "vector_storage_bytes": embed_stats.storage_bytes,
        "dim": EMBED_DIM,
    }

    lazy_index_seconds = chunk_seconds + bm25_seconds + embed_stats.seconds
    lazy_index_storage = bm25_bytes + embed_stats.storage_bytes

    # ── 3. 社区检测 + 完整 GraphRAG 索引成本 ─────────────────────────────
    graph, entity_chunks = build_cooccurrence_graph(chunks)
    t0 = time.perf_counter()
    communities, n_top_cells = detect_communities(graph)
    community_seconds = time.perf_counter() - t0
    n_l0 = sum(1 for c in communities if c.level == 0)
    n_l1 = sum(1 for c in communities if c.level == 1)
    print(
        f"[社区] 共现图 {len(graph)} 实体 | louvain {community_seconds:.3f}s | "
        f"L0 顶层分区 {n_top_cells}（达标二次切分）→ L0 {n_l0} + L1 {n_l1} = {len(communities)} 社区",
        flush=True,
    )
    result["communities"] = {
        "n_entities": len(graph),
        "seconds": round(community_seconds, 4),
        "n_top_cells": n_top_cells,
        "n_l0": n_l0,
        "n_l1": n_l1,
        "n_total": len(communities),
    }

    # ── 4. LLM 实测样本（可选）───────────────────────────────────────────
    extract_cost = LlmUnitCost(False, 0, 0.0, 0.0, 0.0)
    report_cost = LlmUnitCost(False, 0, 0.0, 0.0, 0.0)
    llm_meta: dict[str, object] = {"available": False}
    if not args.no_llm:
        base_url, model, api_key, cfg_source = _resolve_llm_config()
        if base_url:
            client = ChatClient(base_url, model, api_key)
            if client.probe():
                client.chat_json("预热", '只输出 JSON：{"ok": true}')  # 预热不计入样本
                extract_cost = _sample_extract_cost(client, chunks)
                report_cost = _sample_report_cost(client, communities)
                llm_meta = {
                    "available": True,
                    "base_url": base_url,
                    "model": model,
                    "config_source": cfg_source,
                    "calls": client.calls,
                    "prompt_tokens": client.prompt_tokens,
                    "completion_tokens": client.completion_tokens,
                    "median_latency_s": round(statistics.median(client.latencies), 3) if client.latencies else None,
                }
            else:
                print(f"[降级] LLM 端点不可用：{base_url}，全部走模板估算", flush=True)
            client.close()
        else:
            print("[降级] 未配置 LLM（OA_LLM_BASE_URL），全部走模板估算", flush=True)
    result["llm"] = llm_meta
    result["llm_unit_cost"] = {
        "extract": _cost_payload(extract_cost),
        "report": _cost_payload(report_cost),
    }

    # ── 5. 完整 GraphRAG 索引成本（外推）────────────────────────────────
    # 实体抽取：每 chunk 1 次调用，prompt=EXTRACT_TEMPLATE(text)
    extract_prompt_tokens = [est_tokens(EXTRACT_TEMPLATE.format(text=c.text)) for c in chunks]
    extract_in_total = sum(extract_prompt_tokens)
    extract_out_total = len(chunks) * EXTRACT_OUT_EST_TOKENS
    report_prompt_tokens = [c.est_prompt_tokens for c in communities]
    report_in_total = sum(report_prompt_tokens)
    report_out_total = len(communities) * REPORT_OUT_EST_TOKENS
    if extract_cost.measured:
        extract_latency, _ = extract_cost.scale(int(statistics.median(extract_prompt_tokens)), EXTRACT_OUT_EST_TOKENS)
        extract_time = extract_latency * len(chunks)
        extract_tokens_est = extract_in_total + extract_out_total
    else:
        # 无实测：按每 chunk 抽取 30s 保守假设（qwen3:8b 级本机口径，见报告口径声明）
        extract_time = len(chunks) * 30.0
        extract_tokens_est = extract_in_total + extract_out_total
    if report_cost.measured:
        report_latency, _ = report_cost.scale(int(statistics.median(report_prompt_tokens)), REPORT_OUT_EST_TOKENS)
        report_time = report_latency * len(communities)
        report_tokens_est = report_in_total + report_out_total
    else:
        report_time = len(communities) * 35.0
        report_tokens_est = report_in_total + report_out_total
    full_index_seconds = extract_time + report_time + community_seconds
    full_index_tokens = extract_tokens_est + report_tokens_est
    entity_store_bytes = len(chunks) * 2048
    reports_bytes = len(communities) * 3072
    full_storage = lazy_index_storage + entity_store_bytes + reports_bytes
    result["full_index_estimate"] = {
        "extract_calls": len(chunks),
        "extract_est_seconds": round(extract_time, 1),
        "report_calls": len(communities),
        "report_est_seconds": round(report_time, 1),
        "community_seconds": round(community_seconds, 4),
        "total_est_seconds": round(full_index_seconds, 1),
        "total_est_tokens": full_index_tokens,
        "extra_storage_bytes": entity_store_bytes + reports_bytes,
        "total_storage_bytes": full_storage,
        "extract_measured": extract_cost.measured,
        "report_measured": report_cost.measured,
    }

    # ── 6. 查询期对比 ───────────────────────────────────────────────────
    queries = _make_queries()
    entity_to_chunks: dict[str, list[int]] = {e: sorted(set(v)) for e, v in entity_chunks.items()}
    lazy_latencies: list[float] = []
    query_embedder: OllamaEmbedder | None = None
    query_vec_ok = False
    if not args.no_embed:
        query_embedder = OllamaEmbedder("http://127.0.0.1:11434")
        query_vec_ok = query_embedder.probe()
    for query_text, _intent in queries:
        t0 = time.perf_counter()
        bm_ranking = [cid for cid, _ in bm25.search(tokenize(query_text), RECALL_POOL)]
        if query_vec_ok and query_embedder is not None:
            q_vec = query_embedder.embed([query_text])[0]
            vec_scores = [(_cosine(q_vec, chunk_vectors[cid]), cid) for cid in range(len(chunks))]
            vec_scores.sort(reverse=True)
            vec_ranking = [cid for _, cid in vec_scores[:RECALL_POOL]]
        else:
            vec_ranking = []
        fused = rrf_fuse([r for r in (bm_ranking, vec_ranking) if r], top_k=RECALL_POOL)
        # 图扩展（lite：命中 chunk 的实体 → 共现实体邻接 chunk）
        seed_entities: set[str] = set()
        for cid, _ in fused[:5]:
            seed_entities |= chunks[cid].entities
        expand_scores: dict[int, float] = defaultdict(float)
        for cid, _ in fused:
            for ent in chunks[cid].entities:
                for nbr in entity_to_chunks.get(ent, []):
                    if nbr != cid:
                        expand_scores[nbr] += 1.0 / (RRF_K + len(expand_scores) + 1)
        expansion = sorted(expand_scores.items(), key=lambda kv: kv[1], reverse=True)[:GRAPH_EXPAND_CAP]
        _final = rrf_fuse([[cid for cid, _ in fused], [cid for cid, _ in expansion]], top_k=5)
        lazy_latencies.append((time.perf_counter() - t0) * 1000)
    if query_embedder is not None:
        query_embedder.close()
    lazy_query_median_ms = statistics.median(lazy_latencies)
    # 完整 GraphRAG 查询外推：local=1 次报告上下文调用；global=top-8 社区 map + reduce
    if report_cost.measured:
        local_latency, local_tokens = report_cost.scale(900, 150)
        global_latency, global_tokens = report_cost.scale(8 * 400, 200)
    else:
        local_latency, local_tokens = 20.0, 1050
        global_latency, global_tokens = 90.0, 3400
    result["query"] = {
        "n_queries": len(queries),
        "lazy_median_ms": round(lazy_query_median_ms, 2),
        "lazy_with_vector": query_vec_ok,
        "lazy_latencies_ms": [round(v, 2) for v in lazy_latencies],
        "full_local_search_est_ms": round(local_latency * 1000, 1),
        "full_global_search_est_ms": round(global_latency * 1000, 1),
        "full_local_search_est_tokens": local_tokens,
        "full_global_search_est_tokens": global_tokens,
    }

    # ── 7. 汇总矩阵 ─────────────────────────────────────────────────────
    result["matrix"] = {
        "index_seconds": {"lazy": round(lazy_index_seconds, 2), "full_est": round(full_index_seconds, 1)},
        "index_tokens": {"lazy_embed_tokens": corpus_tokens, "full_llm_est_tokens": full_index_tokens},
        "index_llm_calls": {"lazy": 0, "full_est": len(chunks) + len(communities)},
        "index_storage_bytes": {"lazy": lazy_index_storage, "full_est": full_storage},
        "query_median_ms": {
            "lazy": round(lazy_query_median_ms, 2),
            "full_local_est": round(local_latency * 1000, 1),
            "full_global_est": round(global_latency * 1000, 1),
        },
        "cost_ratio_time": round(full_index_seconds / max(lazy_index_seconds, 1e-9), 1),
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[落盘] {args.output}", flush=True)
    _print_matrix(result)
    return 0


def _resolve_llm_config() -> tuple[str, str, str | None, str]:
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


def _sample_extract_cost(client: ChatClient, chunks: list[ChunkData]) -> LlmUnitCost:
    """实测 3 个中位长度 chunk 的实体抽取调用。"""
    samples = pick_median_samples(chunks, N_EXTRACT_SAMPLES, key=lambda c: len(c.text))
    prompt_tokens: list[float] = []
    completion_tokens: list[float] = []
    latencies: list[float] = []
    for chunk in samples:
        content, err = client.chat_json(_SYSTEM_EXTRACT, EXTRACT_TEMPLATE.format(text=chunk.text[:600]))
        if err:
            print(f"[降级] 抽取样本失败：{err}", flush=True)
            continue
        prompt_tokens.append(est_tokens(EXTRACT_TEMPLATE.format(text=chunk.text[:600])))
        completion_tokens.append(est_tokens(content))
        latencies.append(client.latencies[-1])
    if not latencies:
        return LlmUnitCost(False, 0, 0.0, 0.0, 0.0)
    return LlmUnitCost(
        True,
        len(latencies),
        statistics.median(prompt_tokens),
        statistics.median(completion_tokens),
        statistics.median(latencies),
    )


def _sample_report_cost(client: ChatClient, communities: list[Community]) -> LlmUnitCost:
    """实测 5 个中位规模社区的真实报告生成。"""
    samples = pick_median_samples(communities, N_REPORT_SAMPLES, key=lambda c: len(c.members))
    prompt_tokens: list[float] = []
    completion_tokens: list[float] = []
    latencies: list[float] = []
    for community in samples:
        prompt = REPORT_TEMPLATE.format(
            n_entities=len(community.members),
            entities="、".join(community.members[:40]),
            n_relations=len(community.members),
            relations=community.digest,
        )
        content, err = client.chat_json(_SYSTEM_REPORT, prompt)
        if err:
            print(f"[降级] 报告样本失败：{err}", flush=True)
            continue
        prompt_tokens.append(est_tokens(prompt))
        completion_tokens.append(est_tokens(content))
        latencies.append(client.latencies[-1])
    if not latencies:
        return LlmUnitCost(False, 0, 0.0, 0.0, 0.0)
    return LlmUnitCost(
        True,
        len(latencies),
        statistics.median(prompt_tokens),
        statistics.median(completion_tokens),
        statistics.median(latencies),
    )


def _cost_payload(cost: LlmUnitCost) -> dict[str, object]:
    return {
        "measured": cost.measured,
        "n_samples": cost.n_samples,
        "median_prompt_est_tokens": round(cost.median_prompt_tokens, 1),
        "median_completion_est_tokens": round(cost.median_completion_tokens, 1),
        "median_latency_s": round(cost.median_latency_s, 3),
    }


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _make_queries() -> list[tuple[str, str]]:
    return [
        ("滨河线故障停电原因", "local"),
        ("抢修一班负责哪些区段", "local"),
        ("开发区线工业区段设备台账", "local"),
        ("本月各馈线跳闸情况汇总", "global"),
        ("城东1号配变供电的用户", "local"),
        ("朝阳线故障处置效率", "global"),
    ]


def _print_matrix(result: dict[str, object]) -> None:
    m = result["matrix"]
    assert isinstance(m, dict)
    print("\n## 成本矩阵（markdown）\n", flush=True)
    print("| 指标 | LazyGraphRAG（默认档） | 完整 GraphRAG（预估） |", flush=True)
    print("| --- | --- | --- |", flush=True)
    idx_s = m["index_seconds"]
    idx_t = m["index_tokens"]
    idx_c = m["index_llm_calls"]
    idx_st = m["index_storage_bytes"]
    q = m["query_median_ms"]
    print(f"| 索引耗时 | {idx_s['lazy']}s（实测） | {idx_s['full_est']}s（外推） |", flush=True)
    print(
        f"| 索引 token | 嵌入 {idx_t['lazy_embed_tokens']:,} | LLM ≈{idx_t['full_llm_est_tokens']:,}（外推） |",
        flush=True,
    )
    print(f"| 索引 LLM 调用 | {idx_c['lazy']} | ≈{idx_c['full_est']} |", flush=True)
    print(
        f"| 索引存储 | {idx_st['lazy'] / MB:.2f}MB（实测口径） | {idx_st['full_est'] / MB:.2f}MB（外推口径） |",
        flush=True,
    )
    print(
        f"| 查询延迟(中位) | {q['lazy']}ms（实测）"
        f" | local {q['full_local_est']}ms / global {q['full_global_est']}ms（外推） |",
        flush=True,
    )
    print(f"| 成本倍数（时间） | 1x | ≈{m['cost_ratio_time']}x |", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
