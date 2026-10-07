"""kill -9 断点续跑用例的子进程 worker：跑 M2 full 流水线至 FakeModelPort 挂起点，等待父进程强杀。

用法（父进程 tests/integration/test_pipeline_crash_resume.py 调用，勿手工运行）：
    python -m tests.integration.pipeline_crash_worker --tenant <uuid> --document <uuid> \
        --hang-on-call 2 --lease-seconds 1

平台差异：父进程以 proc.kill() 强杀——Windows=TerminateProcess（硬杀，无清理机会，等价
kill -9），POSIX=SIGKILL；子进程被杀时刻 = 首个关键词候选已落库、下一次 LLM 调用挂起中、
extract 步 running、租约未过期。断点续跑三支柱：残留租约到期接管 + fact_key 业务键幂等 +
审核单 uk_review_one_open 幂等。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

if sys.platform == "win32":  # psycopg 异步要求 Selector 事件循环（同 tests/kb 纪律）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.kb.business.kb_extraction import load_seed_catalog
from services.kb.business.kb_pipeline import M2_FULL_STEPS, PipelineRunReport, run_pipeline
from services.kb.retrieval.embed import EmbeddingUnavailableError, OllamaEmbedder
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  # 全模块 ORM 入 metadata（子进程独立需要）
from services.platform.llm.gateway import FakeModelPort
from services.review.business.candidates import ReviewTicketService

PW = "http://ontology-agent.local/o/t1/power#"


def _seed_class_index(iri: str) -> int:
    """K24 序号口径（docs/Agent/13 §30）：种子类在声明序（=extract_v3 目录渲染序=映射序）中的 1 起序号。"""
    return next(i for i, (c_iri, _, _) in enumerate(load_seed_catalog().classes, start=1) if c_iri == iri)


# 场景：4 节 ×~1450 字符 → 语义分块（512±128 token）每节必出 ≥2 chunk；关键词居节首、
# 远离 10% 重叠搬运边界（保证每个关键词只出现在一个 chunk → 候选=4、无跨 chunk 重复证据）。
# K24：extract_v3 编号目录制下模型只回类序号（整数），解析侧映射回 IRI——IRI 直出会被硬幻觉门禁误剪。
SCENE_KEYWORDS = {
    "馈线F001": _seed_class_index(f"{PW}Feeder"),
    "工单OO-123456": _seed_class_index(f"{PW}OutageOrder"),
    "变压器T-09": _seed_class_index(f"{PW}Transformer"),
    "恢复送电操作单": _seed_class_index(f"{PW}RestorePower"),
}
SCENE_PROPERTIES = {"工单OO-123456": {"orderNo": "OO-123456", "hasStatus": "created"}}
_PAD = "台账例行核对记录保持原文以便语义分块与检索评估。" * 40


def build_scene_content() -> str:
    """联调文档：标题 + 四节（每节关键词句 + ~1440 字符台账填充）。"""
    sections = [
        f"## 第{index + 1}节 {keyword}\n{keyword}为本次登记对象。\n{_PAD}\n"
        for index, keyword in enumerate(SCENE_KEYWORDS)
    ]
    return "# 停电处置联调场景\n" + "\n".join(sections)


def make_scene_model(hang_on_call: int | None = None) -> FakeModelPort:
    """确定性场景模型：同输入恒同输出——父子进程「不重不漏」断言的依据。"""
    return FakeModelPort(
        keyword_classes=SCENE_KEYWORDS,
        keyword_properties=SCENE_PROPERTIES,
        hang_on_call=hang_on_call,
    )


def make_failing_embedder() -> OllamaEmbedder:
    """降级嵌入（调用必抛）：kill -9 用例聚焦 extract/align/validate，embed 走 BM25-only 软降级。"""
    embedder = OllamaEmbedder("http://localhost:9", timeout=0.1)

    async def _raise(texts):  # noqa: ANN001
        raise EmbeddingUnavailableError("kill -9 场景：嵌入不可用（BM25-only 软降级）")

    embedder.embed = _raise  # type: ignore[method-assign]
    return embedder


async def _instant_backoff(attempt: int) -> None:
    return None  # 测试不等待真实退避（30s 起）


async def run_scene_pipeline(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    hang_on_call: int | None,
    lease_seconds: int,
) -> PipelineRunReport:
    """场景流水线入口（父子进程共用同一份配置，保证输出确定性）。"""
    return await run_pipeline(
        session_factory,
        tenant_id=tenant_id,
        document_id=document_id,
        embedder=make_failing_embedder(),
        model=make_scene_model(hang_on_call),
        review=ReviewTicketService(session_factory),
        steps=M2_FULL_STEPS,
        backoff=_instant_backoff,
        lease_seconds=lease_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="kill -9 断点续跑用例 worker")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--document", required=True)
    parser.add_argument("--hang-on-call", type=int, default=None)
    parser.add_argument("--lease-seconds", type=int, default=1)
    args = parser.parse_args()
    engine = create_async_engine(Settings().pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    report = asyncio.run(
        run_scene_pipeline(
            factory,
            tenant_id=uuid.UUID(args.tenant),
            document_id=uuid.UUID(args.document),
            hang_on_call=args.hang_on_call,
            lease_seconds=args.lease_seconds,
        )
    )
    print(f"worker finished: status={report.document_status}")
    asyncio.run(engine.dispose())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
