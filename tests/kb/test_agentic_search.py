# tests/kb/test_agentic_search.py
"""Agentic RAG v1 用例（A0 服务端代跑纯规则档；AgenticRAG优化方案 §3/§8.1 冻结契约）。

断言目标：
- 纯函数：decide 三分支（寒暄 skip / 工单号 deterministic_task / default_retrieve）、
  grade 四分支（hit_count_zero / score_below_threshold / span_missing / pass，阈值边界）、
  rewrite 命中（「配变」→「配电变压器」别名方向 + 真实种子「变压」→「变压器」包含方向）
  与不可归一（None）；
- 服务级（stub 注入检索回调，零外部依赖）：寒暄 skip 零召回、单轮 pass、别名改写后 pass
  （rounds 两步 + rewrite_basis）、两轮 fail → degraded="agentic_exhausted"、不可改写单轮
  即降级、max_rounds=1、explain_trace_id 前缀；
- 服务级（PG 夹具，不可达即 skip 的既有纪律）：KnowledgeSearchService / POST /kb/search
  路由直调的 skip 徽标、degraded、agentic=False 零行为变化（响应 agentic 恒 None）。
  单轮 pass / 改写后 pass 的循环机制不走 PG——真实 BM25 ts_rank 分值不可控（常低于评级
  阈值 0.15），确定性循环断言由 stub 注入承担（PG 连接超时的禁止挂死预案同源）。

环境纪律：纯函数/桩用例零外部依赖；集成用例直连本地 PG（不可达即跳过，会话内只探测一次
防逐用例连接超时拖挂套件）。psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor
不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from fastapi import Request
from rdflib import Graph
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.kb.api.kb import search as kb_search_route
from services.kb.api.schemas.kb import (
    KbAgenticRoundOut,
    KbAgenticTraceOut,
    KbSearchIn,
    KbSearchOut,
    KbUsageOut,
)
from services.kb.business import search_service as kb_search_service_module
from services.kb.business.agentic import (
    EXPLAIN_TRACE_PREFIX,
    GRADE_SCORE_THRESHOLD,
    AgenticRound,
    AgenticTrace,
    decide,
    grade,
    rewrite,
    run_agentic_search,
)
from services.kb.business.kb_extraction import SeedCatalog
from services.kb.business.search_service import KnowledgeSearchService
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.retrieval.graph import ClassHierarchy
from services.kb.retrieval.retrieve import HybridSearchResult, SearchHit
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  全模块 ORM 入 metadata（FK 解析）
from services.platform.deps import Principal

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 迷你种子目录（rewrite 注入用）：唯一类「配电变压器」——任务点名别名方向的确定性载体
MINI_CATALOG = SeedCatalog(
    classes=(("urn:test:transformer", "配电变压器", "Transformer"),),
    class_iris=frozenset({"urn:test:transformer"}),
    properties=(),
    shapes_graph=Graph(),
)


def _hit(score: float, span: list[int] | None) -> SearchHit:
    return SearchHit(chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), content="x", score=score, span=span)


def _ch_hit(score: float, span: list[int] | None, channels: list[str]) -> SearchHit:
    """带通道标记的命中（hybrid_search 输出形态：channels 由 RRF 融合回填，评级据此归一）。"""
    return SearchHit(
        chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), content="x", score=score, span=span, channels=channels
    )


# ── 纯函数：decide / grade / rewrite（零外部依赖）─────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("你好", ("retrieval_skipped", "smalltalk_pattern")),
        ("谢谢！", ("retrieval_skipped", "smalltalk_pattern")),
        ("你好呀", ("retrieval_skipped", "smalltalk_pattern")),
        ("在吗", ("retrieval_skipped", "smalltalk_pattern")),
        ("hello!", ("retrieval_skipped", "smalltalk_pattern")),
        ("😀😀😀", ("retrieval_skipped", "smalltalk_pattern")),  # 纯表情
    ],
)
def test_decide_寒暄词面与纯表情_跳过检索(query: str, expected: tuple[str, str]) -> None:
    assert decide(query) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("查一下OO-123456的进度", ("retrieval_required", "deterministic_task")),
        ("GD-20240101-0001 停电原因", ("retrieval_required", "deterministic_task")),
        ("你好，查一下OO-123456", ("retrieval_required", "deterministic_task")),  # 组合句不判寒暄
    ],
)
def test_decide_工单号模式_确定性任务免判别直查(query: str, expected: tuple[str, str]) -> None:
    assert decide(query) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("配变停电影响哪些客户", ("retrieval_required", "default_retrieve")),
        ("oo-123456", ("retrieval_required", "default_retrieve")),  # 大小写敏感（同 SHACL pattern）
        ("哈哈哈", ("retrieval_required", "default_retrieve")),  # 词面集合外不误判寒暄
    ],
)
def test_decide_其余查询_默认检索(query: str, expected: tuple[str, str]) -> None:
    assert decide(query) == expected


def test_grade_命中数为零_判hit_count_zero() -> None:
    assert grade([]) == ("fail", "hit_count_zero")


def test_grade_top1分低于阈值_判score_below_threshold() -> None:
    # Arrange/Act：top1=0.10 < GRADE_SCORE_THRESHOLD=0.15（待 PoC 标定占位值）
    verdict, reason = grade([_hit(GRADE_SCORE_THRESHOLD - 0.05, [0, 1])])
    # Assert
    assert (verdict, reason) == ("fail", "score_below_threshold")


def test_grade_阈值边界_等于阈值即过() -> None:
    # 0.15 本身不判低分（严格小于），span 在场 → pass
    assert grade([_hit(GRADE_SCORE_THRESHOLD, [0, 1])]) == ("pass", "pass")


def test_grade_引用span全缺_判span_missing() -> None:
    verdict, reason = grade([_hit(0.5, None), _hit(0.4, None)])
    assert (verdict, reason) == ("fail", "span_missing")


def test_grade_分数与span齐备_判pass() -> None:
    assert grade([_hit(0.5, [0, 3]), _hit(0.2, None)]) == ("pass", "pass")  # top1 有 span 即可


# ── F1 回归：归一化评级（RRF 分口径对齐，OCR 缺陷 F1）──────────────────────────


def test_grade_F1两路RRF量级_归一满分判pass() -> None:
    # 两路（bm25+vector 各 0.5，k=60）top1 双通道 rank1：RRF 分 = 1.0/61 ≈ 0.0164（生产可达量级）。
    # 修复前按绝对阈值 0.15 恒 fail/score_below_threshold（pass 快速返回成死代码）。
    hit = _ch_hit(1.0 / 61, [0, 1], ["bm25", "vector"])
    assert grade([hit]) == ("pass", "pass")


def test_grade_F1归一化后低分_判score_below_threshold() -> None:
    # 0.001 相对两路理论满分 1.0/61 归一 ≈ 0.061 < 0.5 → fail（阈值仍拦截真实低分）
    hit = _ch_hit(0.001, [0, 1], ["bm25", "vector"])
    assert grade([hit]) == ("fail", "score_below_threshold")


def test_grade_F1嵌入路降级单路通道_归一满分判pass() -> None:
    # BM25-only（vector_unavailable 降级）：通道集单路 → 理论满分 0.5/61，top1 rank1 归一 = 1.0 → pass
    # （降级场景评级不因换算基准漂移恒挂，降级事实由 degraded_reasons 透传表达）
    hit = _ch_hit(0.5 / 61, [0, 1], ["bm25"])
    assert grade([hit]) == ("pass", "pass")


def test_grade_F1含图路三路权重表_归一满分判pass() -> None:
    # 图路命中 → 权重表切三路（0.4/0.4/0.6，Σ=1.4）：top1 全通道 rank1 = 1.4/61 → 归一 1.0 → pass
    hit = _ch_hit(1.4 / 61, [0, 1], ["bm25", "vector", "graph"])
    assert grade([hit]) == ("pass", "pass")


def test_grade_F1显式通道参数优先于hits推断() -> None:
    # 调用方已知当轮参与通道时显式传入（覆盖 hits 自带 channels 的近似推断）：
    # 同一 top1 分（单路 rank1=0.5/61），按两路满分归一=1.0 → pass；按三路满分（Σw=1.4）归一
    # ≈0.357 < 0.5 → fail——归一基准随通道集变化，阈值语义统一。
    hit = _ch_hit(0.5 / 61, [0, 1], ["bm25"])
    assert grade([hit], channels=["bm25", "vector"]) == ("pass", "pass")
    assert grade([hit], channels=["bm25", "vector", "graph"]) == ("fail", "score_below_threshold")


async def test_rewrite_别名归一命中_配变归一配电变压器() -> None:
    # 任务点名方向：「配变」字符按序含于规范标签「配电变压器」→ 替换 + 依据带标签
    assert await rewrite("配变 停电原因", catalog=MINI_CATALOG) == ("配电变压器 停电原因", "term_alias:配电变压器")


async def test_rewrite_真实种子_包含方向归一() -> None:
    # 「变压」⊂「变压器」（match_seed_class 字面包含命中），改写依据记录规范标签
    assert await rewrite("变压 停电") == ("变压器 停电", "term_alias:变压器")


@pytest.mark.parametrize(
    "query",
    [
        "今天天气怎么样",  # 无种子命中
        "配电变压器 停电",  # 已是规范术语（精确命中不改写）
        "馈线F001停电",  # 标签⊂查询词方向不改写（不截断含编号实体）
        "完全无关词",
    ],
)
async def test_rewrite_不可归一_返回None(query: str) -> None:
    assert await rewrite(query, catalog=MINI_CATALOG) is None


async def test_rewrite_真实种子_规范查询不改写_返回None() -> None:
    # 默认种子（seeds/power_seed.ttl）下的回归护栏：规范术语/含编号实体不被误改
    assert await rewrite("馈线F001停电") is None
    assert await rewrite("变压器 故障") is None  # 故障在否定词表（泛词不升类名）


# ── F3/F4 回归：规范术语不改写守卫 + 词段级替换定位（OCR 缺陷 F3/F4）─────────────


@pytest.mark.parametrize("query", ["停电事件", "停电事件，请确认"])
async def test_rewrite_F3真实种子_规范术语exact命中_不落二级误改写(query: str) -> None:
    # 「停电事件」exact 命中 OutageEvent 后曾被二级按序包含误改写为「停电确认事件」
    # （语义收窄 + trace 谎报别名归一）——F3 守卫后整词段跳过二级，返回 None 不改写。
    assert await rewrite(query) is None


async def test_rewrite_F3真实种子_英文本地名exact_不改写() -> None:
    # 「OutageEvent」exact 命中 pw:OutageEvent 英文本地名（kb_extraction._norm 同形归一）→ 守卫跳过
    assert await rewrite("OutageEvent 已确认") is None


async def test_rewrite_F4真实种子_词段级替换定位_不破坏已扫描词段() -> None:
    # 「配电」需归一为「配电线路」，但裸 str.replace(term, target, 1) 按子串替换会落在
    # 首词段「配电线路故障」内部 → 「配电线路线路故障，配电」（重复拼接）。
    # F4 改为 _TERM_RE.finditer 首个全等词段区间拼接替换；首词段「配电线路故障」因
    # 「标签 ⊂ 查询词」守卫（F3 扩展）不改写。
    assert await rewrite("配电线路故障，配电") == ("配电线路故障，配电线路", "term_alias:配电线路")


# ── 服务级：run_agentic_search 编排（stub 注入检索回调，零外部依赖）──────────────


async def test_run_寒暄skip_零召回不触碰检索回调() -> None:
    # Arrange
    calls: list[str] = []

    async def search_fn(q: str) -> list[SearchHit]:
        calls.append(q)
        return []

    # Act
    hits, trace = await run_agentic_search("你好", search_fn, max_rounds=2, catalog=MINI_CATALOG)
    # Assert：零召回成本（§5 场景 1），rounds 空时间线
    assert hits == [] and calls == []
    assert (trace.decision, trace.decision_reason) == ("retrieval_skipped", "smalltalk_pattern")
    assert trace.rounds == [] and trace.degraded is None


async def test_run_单轮pass_一轮即返回() -> None:
    # Arrange
    calls: list[str] = []

    async def search_fn(q: str) -> list[SearchHit]:
        calls.append(q)
        return [_hit(0.5, [0, 4])]

    # Act
    hits, trace = await run_agentic_search("馈线停电", search_fn, max_rounds=2, catalog=MINI_CATALOG)
    # Assert
    assert len(hits) == 1 and calls == ["馈线停电"]
    assert [(r.seq, r.action, r.query, r.rewrite_basis, r.grade, r.grade_reason) for r in trace.rounds] == [
        (1, "search", "馈线停电", None, "pass", "pass")
    ]
    assert trace.degraded is None


async def test_run_别名改写后pass_rounds两步带rewrite_basis() -> None:
    # Arrange：第 1 轮零命中，改写（配变→配电变压器）后第 2 轮命中
    calls: list[str] = []

    async def search_fn(q: str) -> list[SearchHit]:
        calls.append(q)
        return [] if len(calls) == 1 else [_hit(0.6, [0, 6])]

    # Act
    hits, trace = await run_agentic_search("配变 停电原因", search_fn, max_rounds=2, catalog=MINI_CATALOG)
    # Assert（§8.1 契约示例同构时间线）
    assert calls == ["配变 停电原因", "配电变压器 停电原因"]
    assert [(r.seq, r.action, r.query, r.rewrite_basis, r.grade, r.grade_reason) for r in trace.rounds] == [
        (1, "search", "配变 停电原因", None, "fail", "hit_count_zero"),
        (2, "rewrite_search", "配电变压器 停电原因", "term_alias:配电变压器", "pass", "pass"),
    ]
    assert trace.degraded is None and len(hits) == 1


async def test_run_两轮仍fail_降级agentic_exhausted() -> None:
    # Arrange：恒零命中且改写可达 → 纠错轮用尽（§5 场景 4：理由回传不编造）
    calls: list[str] = []

    async def search_fn(q: str) -> list[SearchHit]:
        calls.append(q)
        return []

    # Act
    hits, trace = await run_agentic_search("配变 停电原因", search_fn, max_rounds=2, catalog=MINI_CATALOG)
    # Assert
    assert calls == ["配变 停电原因", "配电变压器 停电原因"] and hits == []
    assert trace.degraded == "agentic_exhausted"
    assert [(r.seq, r.action, r.rewrite_basis, r.grade_reason) for r in trace.rounds] == [
        (1, "search", None, "hit_count_zero"),
        (2, "rewrite_search", "term_alias:配电变压器", "hit_count_zero"),
    ]


async def test_run_不可改写_单轮即降级不空转() -> None:
    # Arrange：零命中且查询不可归一（真实默认种子）→ 不重查同一查询，直接降级
    calls: list[str] = []

    async def search_fn(q: str) -> list[SearchHit]:
        calls.append(q)
        return []

    # Act（catalog 不注入=懒加载 seeds/power_seed.ttl 单例）
    hits, trace = await run_agentic_search("完全无关词", search_fn, max_rounds=2)
    # Assert
    assert calls == ["完全无关词"] and hits == []
    assert trace.degraded == "agentic_exhausted" and len(trace.rounds) == 1


async def test_run_max_rounds为1_单轮用尽即降级() -> None:
    # Arrange
    async def search_fn(q: str) -> list[SearchHit]:
        return []

    # Act
    hits, trace = await run_agentic_search("配变 停电原因", search_fn, max_rounds=1, catalog=MINI_CATALOG)
    # Assert：max_rounds=1 → 无纠错轮
    assert hits == [] and trace.degraded == "agentic_exhausted"
    assert [(r.seq, r.action) for r in trace.rounds] == [(1, "search")]


async def test_run_explain_trace_id_前缀加uuid() -> None:
    # Arrange/Act
    _, trace = await run_agentic_search("你好", _never_search, max_rounds=2, catalog=MINI_CATALOG)
    # Assert：前缀 kb-agentic: + 可解析 uuid（§8.1 explain 回放键；knowledge.explain v1.5 数据源）
    assert trace.explain_trace_id.startswith(EXPLAIN_TRACE_PREFIX)
    uuid.UUID(trace.explain_trace_id.removeprefix(EXPLAIN_TRACE_PREFIX))  # 解析失败即测试失败
    assert AgenticTrace(decision="retrieval_required", decision_reason="default_retrieve").explain_trace_id != (
        trace.explain_trace_id
    )  # 每次 trace 独立生成


async def test_run_F1两路RRF量级命中_单轮pass不烧改写轮() -> None:
    # F1 编排级回归：top1 RRF 分 1.0/61≈0.0164（两路 rank1，生产可达量级）在归一化评级下
    # 单轮 pass 快速返回；修复前按绝对阈值 0.15 恒 fail → 白烧一轮改写后 agentic_exhausted。
    calls: list[str] = []

    async def search_fn(q: str) -> list[SearchHit]:
        calls.append(q)
        return [_ch_hit(1.0 / 61, [0, 4], ["bm25", "vector"])]

    hits, trace = await run_agentic_search("配变 停电原因", search_fn, max_rounds=2, catalog=MINI_CATALOG)
    assert calls == ["配变 停电原因"]  # 修复前 calls 两条（改写轮被空烧）
    assert [(r.seq, r.action, r.grade, r.grade_reason) for r in trace.rounds] == [(1, "search", "pass", "pass")]
    assert trace.degraded is None and len(hits) == 1


async def _never_search(q: str) -> list[SearchHit]:  # pragma: no cover - 寒暄路径不应触达
    raise AssertionError(f"寒暄 skip 不应触发检索回调: {q}")


# ── F2 回归：运行时降级理由透传（KnowledgeSearchService，stub 注入零外部依赖）──────


class _NullSession:
    """最小会话桩：once 回调内的装配点（层次/AclPushdown/hybrid）均已 stub，会话对象不被触碰。"""

    async def __aenter__(self) -> _NullSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def test_服务级_F2_agentic_true_运行时降级理由透传(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2：末轮 hybrid_search 的运行时降级理由（vector_unavailable 等）透传 result.degraded。

    修复前 _search_agentic 恒 degraded=False（与 api/kb.py 静态 resolve_mode 重建同一缺陷，
    两入口两套语义）——本用例同时钉：静态路由理由 + 运行时理由合并去重。
    """
    service = KnowledgeSearchService(
        _NullSession,  # type: ignore[arg-type] once 内装配点全 stub，会话不被触碰
        ollama_base_url="http://localhost:9",
        acl_filter_enabled=False,  # 显式关闭：AclPushdown.prepare 短路，不触 SQL
    )

    async def _fake_hierarchy(db: object, tenant_id: uuid.UUID) -> ClassHierarchy:
        return ClassHierarchy()  # 空层次：闭包退化非失败（与生产无发布本体时同形）

    async def _fake_hybrid(query: str, **_: object) -> HybridSearchResult:
        # 模拟 hybrid_search 真实降级形态：mode=global 路由降级 + 嵌入路不可用，命中两路 RRF 量级
        hit = _ch_hit(1.0 / 61, [0, 1], ["bm25", "vector"])
        return HybridSearchResult(
            query=query,
            mode="global",
            degraded=True,
            channels=["bm25", "vector"],
            hits=[hit],
            mode_used="local",
            degraded_reasons=["mode_downgraded:global", "vector_unavailable"],
        )

    monkeypatch.setattr(service, "_class_hierarchy", _fake_hierarchy)
    monkeypatch.setattr(kb_search_service_module, "hybrid_search", _fake_hybrid)

    result = await service.search(tenant_id=uuid.uuid4(), query="馈线停电", agentic=True, mode="global")
    assert result.degraded is True  # 修复前恒 False（嵌入路不可用时响应谎报未降级）
    assert result.degraded_reasons == ["mode_downgraded:global", "vector_unavailable"]  # 静态+运行时合并去重
    assert result.agentic is not None and result.agentic.degraded is None  # trace pass ≠ 基础设施降级
    assert len(result.citations) == 1 and result.channels == ["bm25", "vector"]


# ── 请求/响应模型契约（§8.1 可选字段；旧客户端零影响）───────────────────────────


def test_请求模型_agentic缺省false_max_rounds缺省2() -> None:
    body = KbSearchIn.model_validate({"query": "x"})
    assert body.agentic is False and body.max_rounds == 2
    body_on = KbSearchIn.model_validate({"query": "x", "agentic": True, "max_rounds": 1})
    assert body_on.agentic is True and body_on.max_rounds == 1


def test_请求模型_max_rounds越界_拒绝() -> None:
    with pytest.raises(ValueError):  # §2.3 红线 max_rounds ≤ 2
        KbSearchIn.model_validate({"query": "x", "max_rounds": 3})


def test_响应模型_agentic缺省None_旧客户端零影响() -> None:
    out = KbSearchOut(
        query="x",
        mode="auto",
        mode_used="local",
        degraded=False,
        channels=[],
        latency_ms=1,
        hits=[],
        usage=KbUsageOut(latency_ms=1),
    )
    assert out.agentic is None


def test_响应模型_agentic块_业务trace投影() -> None:
    # 端点同款装配路径：业务 AgenticTrace → DTO（model_validate(model_dump())）
    trace = AgenticTrace(
        decision="retrieval_required",
        decision_reason="default_retrieve",
        rounds=[
            AgenticRound(seq=1, action="search", query="配变 停电原因", grade="fail", grade_reason="hit_count_zero")
        ],
        degraded="agentic_exhausted",
    )
    dto = KbAgenticTraceOut.model_validate(trace.model_dump())
    assert dto.mode == "rule" and dto.degraded == "agentic_exhausted"
    assert dto.rounds[0].grade_reason == "hit_count_zero"
    assert isinstance(dto.rounds[0], KbAgenticRoundOut)


# ── 集成用例：本地 PG（不可达即跳过；夹具纪律同 tests/kb/test_source_context.py）──────

# 会话内只探测一次（PG 不可达时连接超时可观，逐用例重探会拖挂套件——禁止挂死纪律）
_PROBE_STATE = {"done": False, "ok": False}

QUERY = "feeder outage"  # BM25 'simple' 按空格分词（tests/kb 同口径），英文词可确定性命中


@pytest.fixture
async def ag_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """本地 PG 会话工厂；不可达即跳过（同 tests/kb 夹具纪律，会话内缓存探测结果）。"""
    if _PROBE_STATE["done"] and not _PROBE_STATE["ok"]:
        pytest.skip("本地 PG 不可达（会话内已探测），跳过 agentic 集成用例")
    probe = create_async_engine(Settings().pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        _PROBE_STATE.update(done=True, ok=False)
        pytest.skip("本地 PG 不可达，跳过 agentic 检索集成用例")
    await probe.dispose()
    _PROBE_STATE.update(done=True, ok=True)
    engine = create_async_engine(Settings().pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def ag_seeded(ag_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """独立租户 + 单库 + 单文档（英文内容可 BM25 确定性命中）单 chunk（span 在场）。"""
    async with ag_pg() as db, db.begin():
        tenant = TenantORM(name="ag-it-租户", slug=f"ag-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="ag-it-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        content = "feeder outage dispatch note: breaker tripped, crew assigned"
        doc = DocumentORM(
            tenant_id=tenant.id,
            kb_collection_id=collection.id,
            title="ag-it-doc",
            source_type="upload",
            size_bytes=len(content.encode()),
            minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
            checksum_sha256=hashlib.sha256(content.encode()).hexdigest(),
            meta={"content": content},
            status="indexed",
        )
        db.add(doc)
        await db.flush()
        db.add(
            DocumentChunkORM(
                tenant_id=tenant.id,
                document_id=doc.id,
                seq=0,
                content=content,
                token_count=len(content) // 2,
                meta={"span": [0, len(content)]},
            )
        )
    env = {"tenant_id": tenant.id, "collection_id": collection.id}
    yield env
    async with ag_pg() as db, db.begin():  # FK 逆序清理
        for stmt in (
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == env["tenant_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)


async def test_服务级_寒暄skip_citations空且带decision徽标(
    ag_pg: async_sessionmaker[AsyncSession], ag_seeded: dict
) -> None:
    """agentic=true + 寒暄 → 零召回（citations 空数组）+ retrieval_skipped 徽标（§5 场景 1）。"""
    service = KnowledgeSearchService(ag_pg, ollama_base_url="http://localhost:9")  # 向量路不可达即降级 BM25-only
    result = await service.search(tenant_id=ag_seeded["tenant_id"], query="你好", agentic=True)
    assert result.citations == []  # 零召回（判别在检索前短路）
    trace = result.agentic
    assert trace is not None
    assert (trace.decision, trace.decision_reason) == ("retrieval_skipped", "smalltalk_pattern")
    assert trace.rounds == [] and trace.degraded is None


async def test_服务级_agentic_false_零行为变化_无agentic块(
    ag_pg: async_sessionmaker[AsyncSession], ag_seeded: dict
) -> None:
    """零行为变化红线：agentic=False → 现状路径，结果 agentic 恒 None 且检索照常命中。"""
    service = KnowledgeSearchService(ag_pg, ollama_base_url="http://localhost:9")
    result = await service.search(
        tenant_id=ag_seeded["tenant_id"], query=QUERY, kb_id=ag_seeded["collection_id"], top_k=10
    )
    assert result.agentic is None
    assert len(result.citations) == 1  # BM25 命中种子文档（现状路径未受 agentic 改造影响）


async def test_服务级_agentic_true_零命中不可改写_降级(
    ag_pg: async_sessionmaker[AsyncSession], ag_seeded: dict
) -> None:
    """真实种子下不可归一的零命中查询 → 单轮 fail → degraded="agentic_exhausted"（§5 场景 4）。"""
    service = KnowledgeSearchService(ag_pg, ollama_base_url="http://localhost:9")
    result = await service.search(tenant_id=ag_seeded["tenant_id"], query="qqzzxx yvwtzz", agentic=True)
    assert result.citations == []
    trace = result.agentic
    assert trace is not None
    assert trace.degraded == "agentic_exhausted"
    assert [(r.seq, r.action, r.grade, r.grade_reason) for r in trace.rounds] == [
        (1, "search", "fail", "hit_count_zero")
    ]


def _principal(tenant_id: uuid.UUID) -> Principal:
    return Principal({"sub": str(uuid.uuid4()), "tenant_id": str(tenant_id), "typ": "user", "jti": "test-jti"})


def _fake_request() -> Request:
    """最小 Request 桩：app.state 挂 settings（Ollama 9 端口不可达 → 端点向量路自动降级）。"""
    state = SimpleNamespace(settings=SimpleNamespace(ollama_base_url="http://localhost:9", kb_acl_filter_enabled=False))
    return Request({"type": "http", "app": SimpleNamespace(state=state), "headers": [], "query_string": b""})


async def test_端点级_agentic_true_寒暄skip_响应带agentic块(
    ag_pg: async_sessionmaker[AsyncSession], ag_seeded: dict
) -> None:
    """直调 POST /kb/search 路由函数：agentic=true 透传 → hits/citations 空 + agentic 徽标块。"""
    async with ag_pg() as session:
        body = KbSearchIn(query="你好", kb_id=ag_seeded["collection_id"], agentic=True)
        out = await kb_search_route(
            body=body, principal=_principal(ag_seeded["tenant_id"]), request=_fake_request(), session=session
        )
    assert out.hits == [] and out.citations == []
    assert out.agentic is not None
    assert out.agentic.decision == "retrieval_skipped"
    assert out.agentic.explain_trace_id.startswith(EXPLAIN_TRACE_PREFIX)


async def test_端点级_agentic_false_响应agentic恒None(ag_pg: async_sessionmaker[AsyncSession], ag_seeded: dict) -> None:
    """端点零行为变化红线：不传 agentic → 响应无 agentic 块（旧客户端零影响）。"""
    async with ag_pg() as session:
        body = KbSearchIn(query=QUERY, kb_id=ag_seeded["collection_id"], top_k=10)
        out = await kb_search_route(
            body=body, principal=_principal(ag_seeded["tenant_id"]), request=_fake_request(), session=session
        )
    assert out.agentic is None
    assert len(out.hits) == 1  # 现状检索路径照常


async def test_端点级_F2_agentic_true_嵌入路不可用_degraded透传运行时理由(
    ag_pg: async_sessionmaker[AsyncSession], ag_seeded: dict
) -> None:
    """F2 端点侧回归：Ollama 9 端口不可达 → vector 路抛 EmbeddingUnavailableError →
    agentic=true 响应 degraded=true 且 degraded_reasons 含 vector_unavailable。

    修复前 degraded 仅由静态 resolve_mode 重建（mode=auto 无路由降级）→ 恒 False，与端点
    docstring「嵌入路不可用 → BM25-only（degraded=true, reason=vector_unavailable）」矛盾。
    """
    async with ag_pg() as session:
        body = KbSearchIn(query=QUERY, kb_id=ag_seeded["collection_id"], top_k=10, agentic=True)
        out = await kb_search_route(
            body=body, principal=_principal(ag_seeded["tenant_id"]), request=_fake_request(), session=session
        )
    assert out.degraded is True  # 修复前 False（运行时降级被吞）
    assert "vector_unavailable" in out.degraded_reasons
    assert out.agentic is not None
    # BM25-only 单路命中 → 归一满分（F1 口径）→ 首轮评级 pass（不被降级事实误杀）
    assert [(r.grade, r.grade_reason) for r in out.agentic.rounds] == [("pass", "pass")]
    assert len(out.hits) == 1  # BM25 照常命中种子文档
