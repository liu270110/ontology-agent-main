"""knowledge.search 引用率基线（OntRAG §10 检索引用率 + §5 契约的 M2.5 基线评估；integration）。

- 小样例库：8 个电力域黄金文档（8 chunk）+ 3 个干扰文档（9 chunk，占位防 top-k 恒中）；
  chunk 与权威事实（kb_facts.status='authoritative'，模拟人工终审后状态）直接落库，绕过流水线
  （确定性）；本体读模型落 Ontology/OntologyVersion/OntoClass（Feeder/Transformer ⊂ PowerDevice）；
- golden QA 矩阵 26 条（2026-09-28 收口扩充，原 12 条），分支覆盖：
  BM25 词法命中（qa-01~12/14）/ 图路类闭包命中（qa-13：与环网 chunk 词形零重叠，仅
  Feeder→PowerDevice 闭包邻接可达）/ 向量语义命中（qa-15~20：语义改写词形弱重叠或零重叠）/
  ACL 标签面命中与排除（qa-21~26：X-Acl-Tags 同源谓词，OntRAG §4.3，含空标签面 deny-by-default）/
  降级标注（vector_unavailable 注入专查 + mode_downgraded:global 契约专查，两臂通用）；
- 向量臂口径：活体嵌入探测顺序 = ① 生产口径 OllamaEmbedder（OA_OLLAMA_BASE_URL，/api/embed
  协议）→ ② 本机 TEI bge-m3（127.0.0.1:18002，/embed TEI 协议，测试内最小适配器）。探活成功
  （维度==1024）→ 全库播种真向量 + 查询真嵌入（pgvector 余弦真跑，「vector」通道计入 hit@5
  分母）；均不可达 → 注入降级（评估不依赖嵌入服务，同 tests/kb/test_kb.py 纪律），「仅向量」
  行退出分母并以 verdict='skip' 留痕 evaluation_results（降级口径显式标注，不稀释基线）；
- 断言：citation hit@5 ≥ 0.6（首跑即基线，分母=当臂生效行）；ACL 排除行以「开臂孪生行命中」
  交叉证明（同 query 无标签面可召回、有标签面被拒，排除非零命中假象）+ ACL 行全局不变量
  （命中文档要么未标注继承、要么标签面相交）；结果写 evaluation_runs/evaluation_results
  （benchmark_type='retrieval_qa'，08 §7.3 契约 ORM），metrics 记录向量臂口径与逐行分支。

psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
import sys
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from services.iam.data.orm import Tenant as TenantORM
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import EvaluationResult as EvaluationResultORM
from services.kb.data.orm import EvaluationRun as EvaluationRunORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbFact as KbFactORM
from services.kb.retrieval.embed import (
    EMBED_DIM,
    AclPushdown,
    EmbeddingUnavailableError,
    OllamaEmbedder,
    bm25_search,
    set_chunk_embeddings,
    vector_search,
)
from services.kb.retrieval.graph import build_class_hierarchy, expand_graph
from services.kb.retrieval.retrieve import (
    GraphExpansion,
    HybridSearchResult,
    SearchHit,
    hybrid_search,
)
from services.ontology.business.hierarchy_service import get_class_hierarchy
from services.ontology.data.orm import OntoClass as OntoClassORM
from services.ontology.data.orm import Ontology as OntologyORM
from services.ontology.data.orm import OntologyVersion as OntologyVersionORM
from services.platform.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = [pytest.mark.integration]

PW_NS = "http://ontology-agent.local/o/t1/power#"
OB2_OBJECT = "https://ontology-agent.dev/ns/ob2#Object"
BASELINE_VERSION = "power-lite-v2"  # v2：矩阵扩充（26 条）+ 向量臂活体口径（2026-09-28 收口）
HIT_AT_K = 5
BASELINE_FLOOR = 0.6  # 任务口径：首跑即基线，断言下限（实测值随 evaluation_runs 留档）
TEI_BASE_URL = "http://127.0.0.1:18002"  # 本机 TEI bge-m3（/embed TEI 协议；Ollama 协议 404 已实测）

# 黄金语料（chunk_key → (文档标题, chunk 正文)；空格分词适配 'simple' 配置，见模块 docstring）
GOLDEN_DOCS: dict[str, tuple[str, str]] = {
    "feeder_f001": (
        "馈线故障处置手册",
        "馈线 F001 故障隔离流程 保护动作后 调度员 执行 故障隔离 并 记录 保护动作 情况",
    ),
    "feeder_f002": (
        "馈线故障处置手册",
        "馈线 F002 抢修工单 抢修班组 到场 恢复送电 后 归档 抢修工单",
    ),
    "transformer_oil": (
        "变电站巡检规程",
        "变电站 变压器 T01 油温 巡检 油温超限 时 通知 运维 安排 停电检修",
    ),
    "breather_gel": (
        "变电站巡检规程",
        "变电站 呼吸器 巡检 发现 硅胶 变色 更换 硅胶 并 记录",
    ),
    "outage_order": (
        "停电工单管理规范",
        "停电 工单 OID2024101 由 调度员 录入 停电时间 影响 台区 与 恢复送电时间",
    ),
    "repair_audit": (
        "停电工单管理规范",
        "抢修 工单 审核 归档 后 派发 记录 纳入 月度 考核",
    ),
    "device_ledger": (
        "配网设备台账",
        "配网 设备 台账 涵盖 馈线 变压器 开关 计量表 等 电力 设备",
    ),
    # 图路专查锚点：环网单元验收单——与馈线 query 词形零重叠，仅类闭包（PowerDevice）可达
    "ring_unit": (
        "环网单元验收单",
        "环网 单元 H201 铭牌 参数 出厂 编号 核对 归档",
    ),
}

# chunk_key → 权威事实（subject, 本体类 IRI）；层次投影见 CLASS_HIERARCHY（Feeder ⊂ PowerDevice）
GOLDEN_FACTS: dict[str, tuple[str, str]] = {
    "feeder_f001": ("馈线F001", f"{PW_NS}Feeder"),
    "feeder_f002": ("馈线F002", f"{PW_NS}Feeder"),
    "transformer_oil": ("变压器T01", f"{PW_NS}Transformer"),
    "breather_gel": ("呼吸器", f"{PW_NS}Transformer"),
    "outage_order": ("停电工单OID2024101", f"{PW_NS}OutageOrder"),
    "repair_audit": ("抢修工单", f"{PW_NS}OutageOrder"),
    "device_ledger": ("配网设备台账", f"{PW_NS}PowerDevice"),
    "ring_unit": ("环网单元H201", f"{PW_NS}PowerDevice"),
}

# chunk_key → 文档级 ACL 标注（documents.acl_tags jsonb；未列 key=未标注继承租户全员，§4.3 缺省口径）
DOC_ACL_TAGS: dict[str, list[str]] = {
    "feeder_f001": ["dept:power"],
    "feeder_f002": ["dept:power"],
    "outage_order": ["dept:gridops"],
    "breather_gel": ["dept:finance"],
}

# 检索臂（X-Acl-Tags 头语义，api/kb.py _acl_tags_from_request 同构）：
# open=无头（调用方未接入标签面 → AclPushdown no-op 全库）；其余=有头（含空表=显式空标签面）
SEARCH_ARMS: dict[str, list[str] | None] = {
    "open": None,
    "acl-power": ["dept:power"],
    "acl-gridops": ["dept:gridops"],
    "acl-empty": [],
}


@dataclass(frozen=True, slots=True)
class QACase:
    """QA 矩阵行：query → 期望（hit=期望 chunk 入 top5 / exclude=期望文档被排除）。

    branch=分支归类（报告矩阵用）；vector_only=True 的行在降级臂（无活体嵌入）退出分母；
    twin=排除行的开臂孪生行 id（交叉证明：同 query 无标签面可召回，排除非零命中假象）。
    """

    case_id: str
    query: str
    expected_key: str
    branch: str
    arm: str
    expect: str = "hit"  # hit | exclude
    vector_only: bool = False
    twin: str | None = None
    # 图路分支行固定走 BM25+图纯路（向量路注入降级，旧基线口径）：小语料（17 < RECALL_POOL=50）
    # 下活体向量会把全库召成种子、visited 全覆盖挤占图路扩展（生产语料≫池无此伪影），
    # 类闭包可达性断言与向量臂解耦，保证两臂下图路契约稳定可断言。
    pure_graph: bool = False


QA_MATRIX: tuple[QACase, ...] = (
    # ── BM25 词法命中（开臂，词形重叠直召）─────────────────────────────────
    QACase("qa-01", "馈线 F001 故障隔离", "feeder_f001", "bm25", "open"),
    QACase("qa-02", "馈线 F002 抢修工单", "feeder_f002", "bm25", "open"),
    QACase("qa-03", "变压器 油温 巡检", "transformer_oil", "bm25", "open"),
    QACase("qa-04", "硅胶 变色 更换", "breather_gel", "bm25", "open"),
    QACase("qa-05", "停电 工单 录入 台区", "outage_order", "bm25", "open"),
    QACase("qa-06", "抢修 工单 审核 考核", "repair_audit", "bm25", "open"),
    QACase("qa-07", "故障隔离 保护动作", "feeder_f001", "bm25", "open"),
    QACase("qa-08", "恢复送电 归档", "feeder_f002", "bm25", "open"),
    QACase("qa-09", "油温超限 停电检修", "transformer_oil", "bm25", "open"),
    QACase("qa-10", "OID2024101 调度员", "outage_order", "bm25", "open"),
    QACase("qa-11", "派发 记录 考核", "repair_audit", "bm25", "open"),
    QACase("qa-12", "配网 设备 台账", "device_ledger", "bm25", "open"),
    # ── 图路类闭包（qa-13 与 ring_unit 词形零重叠：BM25 零召回 ring_unit，仅 Feeder→PowerDevice
    #    闭包可达；query 全 token 落在种子 feeder_f001 内——websearch_to_tsquery AND 语义）──
    QACase("qa-13", "馈线 F001 调度员 执行", "ring_unit", "graph", "open", pure_graph=True),
    QACase("qa-14", "环网 单元 铭牌 参数", "ring_unit", "bm25", "open"),
    # ── 向量语义命中（语义改写；vector_only=与正文词形零重叠，降级臂退出分母）──
    QACase("qa-15", "变压器 温度 异常 应急 预案", "transformer_oil", "vector", "open", vector_only=True),
    QACase("qa-16", "抢修 班组 完工 报告 存档 要求", "feeder_f002", "vector", "open", vector_only=True),
    QACase("qa-17", "绝缘 油 过热 告警 处置 流程", "transformer_oil", "vector", "open", vector_only=True),
    QACase("qa-18", "干燥剂 颜色 异常 替换 操作 步骤", "breather_gel", "vector", "open", vector_only=True),
    QACase("qa-19", "电网 资产 清册 覆盖 范围", "device_ledger", "vector", "open", vector_only=True),
    QACase("qa-20", "故障 停运 范围 用户 清单 时限", "outage_order", "vector", "open", vector_only=True),
    # ── ACL 标签面（命中=标注文档在标签面内可见 / 排除=异标签或空标签面下被拒）──
    QACase("qa-21", "馈线 F001 故障隔离", "feeder_f001", "acl_hit", "acl-power"),
    QACase("qa-22", "硅胶 变色 更换", "breather_gel", "acl_exclude", "acl-power", "exclude", twin="qa-04"),
    QACase("qa-23", "停电 工单 录入 台区", "outage_order", "acl_hit", "acl-gridops"),
    QACase("qa-24", "停电 工单 录入 台区", "outage_order", "acl_exclude", "acl-empty", "exclude", twin="qa-05"),
    QACase("qa-25", "抢修 工单 审核 考核", "repair_audit", "acl_hit", "acl-empty"),  # 未标注继承=始终可见
    QACase("qa-26", "配网 设备 台账", "device_ledger", "acl_hit", "acl-power"),  # 未标注继承=始终可见
)

# 类层次投影（ontology 读模型；Feeder ⊂ PowerDevice 撑类闭包契约，Transformer 直挂平台顶类——
# 压平闭包扇出：小语料下 PowerDevice 全家邻接会以图权 0.6 淹没词法 top1（实测 rank 6 溢出 top5），
# 生产语料≫闭包邻域无此伪影，测试投影取最小可行层次）
CLASS_HIERARCHY: dict[str, list[str]] = {
    f"{PW_NS}PowerDevice": [OB2_OBJECT],
    f"{PW_NS}Feeder": [f"{PW_NS}PowerDevice"],
    f"{PW_NS}Transformer": [OB2_OBJECT],
    f"{PW_NS}Switch": [f"{PW_NS}PowerDevice"],
    f"{PW_NS}OutageOrder": [OB2_OBJECT],
}

_FILLER_DOCS: list[tuple[str, list[str]]] = [
    (
        "平台运维公告",
        [
            "本周 例行 维护 完成 数据 备份 一致性 校验 与 组件 版本 发布 事项",
            "值班 安排 调整 请 各 组 提前 报备 节假日 值守 名单 与 联系 方式",
            "办公 终端 安全 检查 将于 下周 启动 请 及时 更换 弱 口令 并 锁屏",
        ],
    ),
    (
        "会议纪要汇编",
        [
            "季度 复盘 会议 确定 下 阶段 试点 范围 与 验收 材料 清单 责任 人",
            "跨 组 协同 事项 汇总 接口 文档 评审 时间 待 定 请 关注 邮件 通知",
            "培训 计划 更新 新 员工 入职 课程 与 内部 分享 主题 征集 开放",
        ],
    ),
    (
        "行政管理制度",
        [
            "差旅 报销 流程 调整 发票 张贴 与 审批 链路 以 新 版 指引 为准",
            "固定资产 盘点 启动 各 部门 核对 台账 信息 并 确认 领用 记录",
            "会议室 预约 规则 优化 长时 占用 需 附 说明 释放 空闲 时段",
        ],
    ),
]


# ---------------------------------------------------------------- 向量臂：活体嵌入探测（TEI / Ollama / 降级）


class TeiEmbedder:
    """TEI（text-embeddings-inference）/embed 协议最小适配器（仅测试评估用；生产嵌入客户端=OllamaEmbedder）。

    与 OllamaEmbedder 同契约：embed(texts) 顺序即输出、批量分批、任何失败抛
    EmbeddingUnavailableError（调用方降级口径唯一）。
    """

    def __init__(self, base_url: str, *, timeout: float = 10.0, batch_size: int = 32) -> None:
        self._base_url = base_url.rstrip("/")
        self._batch_size = max(1, batch_size)
        self._client = httpx.AsyncClient(timeout=timeout)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        items = list(texts)
        for start in range(0, len(items), self._batch_size):
            batch = items[start : start + self._batch_size]
            try:
                resp = await self._client.post(f"{self._base_url}/embed", json={"inputs": batch})
            except httpx.HTTPError as exc:
                raise EmbeddingUnavailableError(f"TEI 不可达（{self._base_url}）: {exc}") from exc
            if resp.status_code != 200:
                raise EmbeddingUnavailableError(f"TEI 返回 {resp.status_code}: {resp.text[:200]}")
            try:
                vectors = resp.json()
            except ValueError as exc:
                raise EmbeddingUnavailableError(f"TEI 响应非 JSON: {exc}") from exc
            if (
                not isinstance(vectors, list)
                or len(vectors) != len(batch)
                or any(not isinstance(v, list) for v in vectors)
            ):
                raise EmbeddingUnavailableError("TEI 响应结构异常（期望 [[float]] 与输入等长）")
            out.extend(vectors)
        return out

    async def aclose(self) -> None:
        await self._client.aclose()


async def _probe_live_embedder() -> tuple[str, OllamaEmbedder | TeiEmbedder] | tuple[None, None]:
    """活体嵌入探测：① 生产口径 OllamaEmbedder（OA_OLLAMA_BASE_URL）→ ② 本机 TEI bge-m3。

    探针 = 单文本嵌入且维度==EMBED_DIM（bge-m3 契约）；双双不可达 → (None, None)=降级臂。
    """
    ollama = OllamaEmbedder(Settings().ollama_base_url)
    try:
        vectors = await ollama.embed(["probe"])
        if len(vectors) == 1 and len(vectors[0]) == EMBED_DIM:
            return "ollama", ollama
    except EmbeddingUnavailableError:
        pass
    await ollama.aclose()
    tei = TeiEmbedder(TEI_BASE_URL)
    try:
        vectors = await tei.embed(["probe"])
        if len(vectors) == 1 and len(vectors[0]) == EMBED_DIM:
            return "tei", tei
    except EmbeddingUnavailableError:
        pass
    await tei.aclose()
    return None, None


@pytest.fixture
async def vector_arm() -> AsyncIterator[tuple[str, OllamaEmbedder | TeiEmbedder | None]]:
    """每用例探测一次活体嵌入服务；用例结束关闭客户端（不可达=降级臂口径）。"""
    name, embedder = await _probe_live_embedder()
    yield name, embedder
    if embedder is not None:
        await embedder.aclose()


# ---------------------------------------------------------------- 夹具（样例库 + 本体读模型 + ACL 标注）


@pytest.fixture(scope="module")
def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """一次性测试库会话工厂（ONT-1 批转换：模块级一库，create_all 建全量表含跨模块 FK）。

    变更动因：本文件消费 ontology 读模型 ORM（OntoClassORM/OntologyVersionORM），ONT-1 批
    ORM 增列先行于共享开发库 schema（其 alembic 版本戳失效无法 upgrade）——全列 SELECT 即
    UndefinedColumn；一次性库建表即含新列（tests/agent/pg_testdb.py 同款机制）。同步夹具
    （asyncio.run 驱动建/删库）规避 pytest-asyncio loop 作用域错配；NullPool 每 checkout
    新建连接，各用例独立事件循环安全共享引擎。"""
    from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（跨模块 FK 解析）
    from services.platform.db.base import Base
    from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

    settings = Settings()
    if not asyncio.run(probe_pg(settings.pg_dsn)):
        pytest.skip("本地 PG 不可达，跳过引用率基线评估")
    test_dsn = asyncio.run(create_test_database(settings.pg_dsn))
    engine = create_async_engine(test_dsn, poolclass=NullPool)

    async def _create_all() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # 复刻迁移 0386f2520028 的 pgvector 条件 DDL（embedding 列迁移条件管理、不在 ORM；
            # 扩展不可用时容错跳过——检索侧 vector_ready 自动降级，迁移语义逐字对齐）
            await conn.execute(
                text(
                    "DO $$ BEGIN CREATE EXTENSION IF NOT EXISTS vector; "
                    "EXCEPTION WHEN OTHERS THEN NULL; END $$;"
                )
            )
            if (await conn.execute(text("SELECT to_regtype('vector') IS NOT NULL"))).scalar():
                await conn.execute(
                    text("ALTER TABLE document_chunks ADD COLUMN embedding vector(1024)")
                )
                await conn.execute(
                    text(
                        "CREATE INDEX ix_document_chunks_embedding ON document_chunks "
                        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
                    )
                )

    try:
        asyncio.run(_create_all())
    except BaseException:
        # 建表失败（含 pgvector 条件 DDL）也要 drop 一次性库并还引擎——setup 抛出不走
        # yield 后的清理路径，不兜底即泄漏一次性库（ocr 评审 #1 同款，2026-10-07）
        asyncio.run(drop_test_database(test_dsn))
        asyncio.run(engine.dispose())
        raise
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(drop_test_database(test_dsn))
    asyncio.run(engine.dispose())


_COLUMN_EXISTS_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
    " WHERE table_name = 'documents' AND column_name = 'acl_tags')"
)


_TAG_UPDATE_SQL = text("UPDATE documents SET acl_tags = CAST(:tags AS jsonb) WHERE id = :doc_id")


async def _ensure_acl_state(db: AsyncSession, doc_tags: dict[uuid.UUID, list[str]] | None = None) -> bool:
    """确保 acl_tags 列在位并重灌标注；返回列是否由本用例侧新建（夹具据此决定结束是否撤列）。

    并行 worktree 共库竞态护栏：旧版用例的无条件撤列会把迁移列与行内标注一并清掉——带标签
    检索前调用本 helper（幂等 ADD IF NOT EXISTS + 标注重灌 UPDATE），把「列被外部撤掉」的
    失效窗口从夹具级收窄到语句级；DML/DDL 落在本会话事务内，随 close 回滚无残留。
    """
    existed = bool((await db.execute(_COLUMN_EXISTS_SQL)).scalar())
    if not existed:
        await db.execute(
            text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS acl_tags jsonb NOT NULL DEFAULT '[]'::jsonb")
        )
    if doc_tags:
        for doc_id, tags in doc_tags.items():
            await db.execute(_TAG_UPDATE_SQL, {"tags": json.dumps(tags), "doc_id": doc_id})
    return not existed


@pytest.fixture
async def baseline(
    kb_pg: async_sessionmaker[AsyncSession], vector_arm: tuple[str, OllamaEmbedder | TeiEmbedder | None]
) -> AsyncIterator[dict]:
    """样例库：租户/集合/黄金+干扰文档/chunks/权威事实 + 本体读模型 + ACL 标注 + 向量臂播种。"""
    arm_name, embedder = vector_arm
    async with kb_pg() as db, db.begin():
        created_column = await _ensure_acl_state(db)
        tenant = TenantORM(name="kb-baseline-租户", slug=f"kb-bl-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="kb-bl-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()

        chunk_ids: dict[str, uuid.UUID] = {}
        doc_ids: dict[str, uuid.UUID] = {}
        chunk_texts: list[tuple[uuid.UUID, str]] = []
        for key, (title, content) in GOLDEN_DOCS.items():
            doc_id, chunk_id = await _seed_doc(
                db, tenant_id=tenant.id, collection_id=collection.id, title=title, content=content
            )
            doc_ids[key], chunk_ids[key] = doc_id, chunk_id
            chunk_texts.append((chunk_id, content))
        filler_no = 0
        for title, paragraphs in _FILLER_DOCS:
            for paragraph in paragraphs:
                filler_no += 1
                doc_id, chunk_id = await _seed_doc(
                    db,
                    tenant_id=tenant.id,
                    collection_id=collection.id,
                    title=f"{title}·{filler_no}",
                    content=paragraph,
                )
                chunk_texts.append((chunk_id, paragraph))

        # ACL 标注（列不在 ORM 映射，raw UPDATE 同 tests/kb/test_acl_prefilter.py 纪律）
        for key, tags in DOC_ACL_TAGS.items():
            await db.execute(
                text("UPDATE documents SET acl_tags = CAST(:tags AS jsonb) WHERE id = :doc_id"),
                {"tags": json.dumps(tags), "doc_id": doc_ids[key]},
            )

        for key, (subject, class_iri) in GOLDEN_FACTS.items():
            db.add(
                KbFactORM(
                    tenant_id=tenant.id,
                    document_id=doc_ids[key],
                    chunk_id=chunk_ids[key],
                    fact_type="entity",
                    subject=subject,
                    subject_type=class_iri,
                    canonical_name=subject,
                    aliases=[class_iri],  # align 步口径：类 IRI 入 aliases（检索图路同源素材）
                    confidence=0.900,
                    status="authoritative",  # 模拟终审后权威态（底线 1：candidate 不参与检索）
                    evidence={"source_ref": {"note": "baseline-seed"}},
                )
            )

        # 本体读模型（ontology 模块表；层次投影 = CLASS_HIERARCHY；ontology 先行满足 FK）
        ontology = OntologyORM(
            tenant_id=tenant.id,
            iri_base=PW_NS.rstrip("#"),
            name=f"power-seed-{uuid.uuid4().hex[:8]}",
            status="published",
        )
        db.add(ontology)
        await db.flush()
        version = OntologyVersionORM(
            tenant_id=tenant.id,
            ontology_id=ontology.id,
            version="v1",
            version_no=1,
            artifact_key=f"test://{BASELINE_VERSION}/power.ttl",
            checksum="0" * 64,
        )
        db.add(version)
        await db.flush()
        ontology.current_version_id = version.id  # 发布指针（本表 FK 迁移后置，无约束阻塞）
        for iri, supers in CLASS_HIERARCHY.items():
            db.add(
                OntoClassORM(
                    tenant_id=tenant.id,
                    ontology_id=ontology.id,
                    version_id=version.id,
                    iri=iri,
                    name=iri.rsplit("#", 1)[-1],
                    subclass_of=supers,
                )
            )

        # 向量臂播种：活体嵌入服务在 → 全库真向量（pgvector 余弦真跑的前提；降级臂不播种）
        if embedder is not None and chunk_texts:
            vectors = await embedder.embed([content for _, content in chunk_texts])
            await set_chunk_embeddings(
                db, [(cid, vec, None) for (cid, _), vec in zip(chunk_texts, vectors, strict=True)]
            )

    ids: dict[str, object] = {
        "tenant_id": tenant.id,
        "collection_id": collection.id,
        "chunk_ids": chunk_ids,
        "doc_ids": doc_ids,
        "tags_by_doc": {doc_ids[key]: tags for key, tags in DOC_ACL_TAGS.items()},
        "vector_arm": arm_name,
        "embedder": embedder,
        "created_column": created_column,
    }
    yield ids
    async with kb_pg() as db, db.begin():  # FK 逆序清理（+ 按需撤自建 ACL 列）
        for stmt in (
            delete(EvaluationResultORM).where(EvaluationResultORM.tenant_id == ids["tenant_id"]),
            delete(EvaluationRunORM).where(EvaluationRunORM.tenant_id == ids["tenant_id"]),
            delete(KbFactORM).where(KbFactORM.tenant_id == ids["tenant_id"]),
            delete(OntoClassORM).where(OntoClassORM.tenant_id == ids["tenant_id"]),
            delete(OntologyVersionORM).where(OntologyVersionORM.tenant_id == ids["tenant_id"]),
            delete(OntologyORM).where(OntologyORM.tenant_id == ids["tenant_id"]),
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == ids["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == ids["tenant_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.tenant_id == ids["tenant_id"]),
            delete(TenantORM).where(TenantORM.id == ids["tenant_id"]),
        ):
            await db.execute(stmt)
        if ids["created_column"]:
            await db.execute(text("ALTER TABLE documents DROP COLUMN IF EXISTS acl_tags"))


async def _seed_doc(
    db: AsyncSession, *, tenant_id: uuid.UUID, collection_id: uuid.UUID, title: str, content: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """单文档单 chunk 直插（确定性，绕过流水线）；span=[0, len) 满足出处指针门禁。"""
    doc = DocumentORM(
        tenant_id=tenant_id,
        kb_collection_id=collection_id,
        title=title,
        source_type="upload",
        size_bytes=len(content.encode()),
        minio_key=f"raw-docs/{tenant_id}/{collection_id}/{uuid.uuid4()}/source.md",
        checksum_sha256=hashlib.sha256(f"{title}|{content}".encode()).hexdigest(),
        meta={"content": content},
        status="indexed",
    )
    db.add(doc)
    await db.flush()
    chunk = DocumentChunkORM(
        tenant_id=tenant_id,
        document_id=doc.id,
        seq=0,
        content=content,
        token_count=len(content) // 2,
        meta={"span": [0, len(content)]},
    )
    db.add(chunk)
    await db.flush()
    return doc.id, chunk.id


async def lite_search(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    collection_id: uuid.UUID,
    query: str,
    embedder: OllamaEmbedder | TeiEmbedder | None,
    top_k: int = HIT_AT_K,
    max_hops: int = 2,
    acl_tags: Sequence[str] | None = None,
    acl_doc_tags: dict[uuid.UUID, list[str]] | None = None,
    mode: str = "local",
) -> HybridSearchResult:
    """knowledge.search lite 服务级组合（镜像 api/kb.py search 装配，绕过 HTTP 层）。

    - acl_tags：X-Acl-Tags 头语义（None=无头 no-op；含空表=显式空标签面），经 AclPushdown
      真实 prepare（enabled=True，列存在性探测同生产开关开启路径）；acl_doc_tags=本用例的
      文档标注面，prepare 前经 _ensure_acl_state 自愈（并行撤列竞态护栏）；
    - embedder=None → 向量路注入降级（EmbeddingUnavailableError，评估不依赖嵌入服务的口径）。
    """
    rows = await get_class_hierarchy(db, tenant_id=tenant_id)
    hierarchy = build_class_hierarchy((row.iri, row.name, row.subclass_of) for row in rows)
    if acl_tags is not None:
        await _ensure_acl_state(db, acl_doc_tags)
    acl = await AclPushdown.prepare(db, enabled=True, allowed_tags=acl_tags)

    async def bm25_fn(q: str, k: int) -> list[SearchHit]:
        found = await bm25_search(db, tenant_id=tenant_id, query=q, top_k=k, collection_id=collection_id, acl=acl)
        return [
            SearchHit(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                content=row["content"],
                score=row["score"],
                doc_name=row["doc_name"],
                minio_key=row.get("minio_key"),
                span=row.get("span"),
            )
            for row in found
        ]

    async def vector_fn(q: str, k: int) -> list[SearchHit]:
        if embedder is None:
            raise EmbeddingUnavailableError("baseline：向量路注入降级（活体嵌入服务未探得）")
        embeddings = await embedder.embed([q])
        found = await vector_search(
            db,
            tenant_id=tenant_id,
            query_embedding=embeddings[0],
            top_k=k,
            collection_id=collection_id,
            acl=acl,
        )
        return [
            SearchHit(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                content=row["content"],
                score=row["score"],
                doc_name=row["doc_name"],
                minio_key=row.get("minio_key"),
                span=row.get("span"),
            )
            for row in found
        ]

    async def graph_fn(seeds: Sequence[SearchHit]) -> GraphExpansion:
        return await expand_graph(
            db,
            tenant_id=tenant_id,
            collection_id=collection_id,
            seeds=seeds,
            hierarchy=hierarchy,
            max_hops=max_hops,
            acl=acl,
        )

    return await hybrid_search(query, bm25=bm25_fn, vector=vector_fn, graph=graph_fn, top_k=top_k, mode=mode)


async def test_citation_baseline_hit_at_5(kb_pg: async_sessionmaker[AsyncSession], baseline: dict) -> None:
    """golden QA 矩阵全量跑 knowledge.search lite：hit@5 ≥ 0.6（分母=当臂生效行）+ 结果落 evaluation 契约。"""
    tenant_id: uuid.UUID = baseline["tenant_id"]
    collection_id: uuid.UUID = baseline["collection_id"]
    chunk_ids: dict[str, uuid.UUID] = baseline["chunk_ids"]
    tags_by_doc: dict[uuid.UUID, list[str]] = baseline["tags_by_doc"]
    arm_name: str = baseline["vector_arm"]
    embedder = baseline["embedder"]
    live_arm = embedder is not None
    key_by_id = {cid: key for key, cid in chunk_ids.items()}

    async with kb_pg() as db:
        # 图路契约专查（BM25+图纯路，embedder=None——旧基线口径，类闭包断言与向量臂解耦）：
        # Feeder 种子 → 层次闭包扩展出 PowerDevice 端点（device_ledger/ring_unit）+ subclass_of 边
        graph_result = await lite_search(
            db,
            tenant_id=tenant_id,
            collection_id=collection_id,
            query="馈线 F001 故障隔离",
            embedder=None,
        )
        assert "graph" in graph_result.channels, "图路未生效：channels 缺 graph"
        path_chunk_ids = {cid for path in graph_result.graph_paths for cid in path.chunk_ids}
        assert chunk_ids["device_ledger"] in path_chunk_ids, "层次扩展未命中 PowerDevice 端点"
        assert chunk_ids["ring_unit"] in path_chunk_ids, "层次扩展未命中 ring_unit（PowerDevice 闭包端点）"
        edge_types = {rel.type for path in graph_result.graph_paths for rel in path.rels}
        assert "subclass_of" in edge_types, "类 IRI 链缺 subclass_of 边"

        # 降级标注契约专查（两臂通用）：① 嵌入路故障 → vector_unavailable；② mode=global → 降级 local
        outage = await lite_search(
            db,
            tenant_id=tenant_id,
            collection_id=collection_id,
            query="馈线 F001 故障隔离",
            embedder=None,
        )
        assert outage.degraded is True and "vector_unavailable" in outage.degraded_reasons
        mode_down = await lite_search(
            db,
            tenant_id=tenant_id,
            collection_id=collection_id,
            query="馈线 F001 故障隔离",
            embedder=embedder,
            mode="global",
        )
        assert mode_down.mode_used == "local" and "mode_downgraded:global" in mode_down.degraded_reasons
        assert mode_down.degraded is True

        latencies: list[float] = []
        case_rows: list[dict] = []
        rank_by_case: dict[str, int | None] = {}
        for case in QA_MATRIX:
            face = SEARCH_ARMS[case.arm]
            row_embedder = None if case.pure_graph else embedder
            started = time.perf_counter()
            result = await lite_search(
                db,
                tenant_id=tenant_id,
                collection_id=collection_id,
                query=case.query,
                embedder=row_embedder,
                acl_tags=face,
                acl_doc_tags=tags_by_doc if face is not None else None,
            )
            latencies.append((time.perf_counter() - started) * 1000)
            if case.expect == "exclude":
                # 排除行：期望文档全程缺席 + 开臂孪生行命中（证明排除是 ACL 所致非召回缺失）
                excluded = chunk_ids[case.expected_key] not in {h.chunk_id for h in result.hits}
                twin_rank = rank_by_case.get(case.twin) if case.twin is not None else None
                ok = excluded and twin_rank is not None
                rank = None
            else:
                rank = next(
                    (i for i, hit in enumerate(result.hits, start=1) if hit.chunk_id == chunk_ids[case.expected_key]),
                    None,
                )
                ok = rank is not None
            rank_by_case[case.case_id] = rank
            if face is not None:  # ACL 行全局不变量（deny-by-default）：命中文档未标注继承或标签面相交
                for hit in result.hits:
                    doc_tags = tags_by_doc.get(hit.document_id, [])
                    assert not doc_tags or set(doc_tags) & set(face), (
                        f"{case.case_id}：异标签文档泄漏进命中（tags={doc_tags}, face={face}）"
                    )
            if case.branch == "graph" and case.expect == "hit":
                assert "graph" in result.channels, f"{case.case_id}：图路分支行 channels 缺 graph"
            skipped = case.vector_only and not live_arm
            case_rows.append(
                {
                    "case_id": case.case_id,
                    "query": case.query,
                    "expected": case.expected_key,
                    "branch": case.branch,
                    "arm": case.arm,
                    "expect": case.expect,
                    "vector_only": case.vector_only,
                    "rank": rank,
                    "ok": ok,
                    "skipped": skipped,
                    "latency_ms": latencies[-1],
                    "top_k_keys": [key_by_id.get(h.chunk_id, str(h.chunk_id)) for h in result.hits[:HIT_AT_K]],
                }
            )
            assert skipped or result.mode_used == "local"
            if live_arm and case.case_id == "qa-15":
                # 活体臂向量通道真跑证据：语义改写 query 的向量召回进入 channels（降级臂自然无此前缀）
                assert "vector" in result.channels, "活体嵌入臂 qa-15 向量通道未生效"
        # 末例契约形状断言（citations/answers 全字段）
        final = await lite_search(
            db,
            tenant_id=tenant_id,
            collection_id=collection_id,
            query=QA_MATRIX[0].query,
            embedder=embedder,
        )
        assert final.hits and final.answer is not None
        if live_arm:
            assert final.degraded is False, f"活体嵌入臂常规检索出现非预期降级：{final.degraded_reasons}"
            assert "vector" in final.channels, "活体嵌入臂末例向量通道未生效"
        citation = final.answer.citations
        assert citation and all(cid in chunk_ids.values() for cid in citation)
        assert final.answer.sentences and all(s.citations for s in final.answer.sentences)
        assert 0.0 < final.answer.confidence <= 1.0

    counted = [row for row in case_rows if not row["skipped"]]
    hit_total = sum(1 for row in counted if row["ok"])
    hit_at_5 = round(hit_total / len(counted), 4)
    p50 = round(statistics.median(latencies), 1)
    failed = [row["case_id"] for row in counted if not row["ok"]]
    print(
        f"\n[citation-baseline] vector_arm={arm_name} hit@{HIT_AT_K}={hit_at_5} ({hit_total}/{len(counted)}) "
        f"skipped={len(case_rows) - len(counted)} p50_latency_ms={p50} "
        f"max_latency_ms={round(max(latencies), 1)} version={BASELINE_VERSION}"
    )
    for row in case_rows:
        print(
            f"[case {row['case_id']}] arm={row['arm']} branch={row['branch']} expect={row['expect']} "
            f"rank={row['rank']} ok={row['ok']} skipped={row['skipped']} "
            f"latency_ms={round(row['latency_ms'], 1)} query={row['query']!r} expected={row['expected']} "
            f"top{HIT_AT_K}={row['top_k_keys']}"
        )
    assert hit_at_5 >= BASELINE_FLOOR, f"hit@{HIT_AT_K}={hit_at_5} 低于基线下限 {BASELINE_FLOOR}（未命中：{failed}）"

    # 结果落 evaluation 契约（08 §7.3：run 主指标 + 逐 case result）
    async with kb_pg() as db, db.begin():
        run = EvaluationRunORM(
            tenant_id=tenant_id,
            benchmark_type="retrieval_qa",
            benchmark_version=BASELINE_VERSION,
            trigger_ref={
                "source": "tests/kb/test_citation_baseline.py",
                "note": f"首跑即基线（矩阵 {len(QA_MATRIX)} 条；向量臂={arm_name}"
                + ("，真向量入分母" if live_arm else "，降级口径：仅向量行 skip 退出分母")
                + "）",
            },
            metrics={
                f"hit_at_{HIT_AT_K}": hit_at_5,
                "hit_total": hit_total,
                "qa_total": len(counted),
                "qa_matrix_total": len(QA_MATRIX),
                "skipped_total": len(case_rows) - len(counted),
                "vector_arm": arm_name,
                "p50_latency_ms": p50,
                "max_latency_ms": round(max(latencies), 1),
                "channels": sorted(graph_result.channels),
                "branches": sorted({row["branch"] for row in case_rows}),
            },
            passed=hit_at_5 >= BASELINE_FLOOR,
        )
        db.add(run)
        await db.flush()
        for row in case_rows:
            db.add(
                EvaluationResultORM(
                    tenant_id=tenant_id,
                    run_id=run.id,
                    case_id=row["case_id"],
                    metrics={
                        "rank": row["rank"],
                        f"hit_at_{HIT_AT_K}": row["ok"],
                        "latency_ms": round(row["latency_ms"], 1),
                        "skipped": row["skipped"],
                    },
                    verdict="skip" if row["skipped"] else ("pass" if row["ok"] else "fail"),
                    detail={
                        "query": row["query"],
                        "expected": row["expected"],
                        "branch": row["branch"],
                        "arm": row["arm"],
                        "expect": row["expect"],
                        "top_k": row["top_k_keys"],
                    },
                )
            )
        saved = (await db.execute(select(EvaluationRunORM).where(EvaluationRunORM.id == run.id))).scalar_one()
    assert saved.passed is True
    assert saved.metrics[f"hit_at_{HIT_AT_K}"] == hit_at_5
    assert saved.metrics["vector_arm"] == arm_name
