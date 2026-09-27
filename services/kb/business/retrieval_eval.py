"""检索质量评估执行器（08 §7.3 评估结果契约 v1；OntRAG §6.9 评估驱动）。

golden 集=seeds/golden/power_retrieval_golden.jsonl（30 例：local 12/global 10/drift 8，
benchmark_version='power-golden-30@v1'）。逐例调 KnowledgeSearchService.search（mode 透传）：

- 指标：hit@k（首个期望文档进前 k 引用）、MRR（首个期望文档排名倒数均值）、
  citation_coverage（有引用用例占比）；按 mode 分层（metrics["by_mode"]）；
- 期望文档映射：golden 的 expected_doc_ids 是语料 stem（如 ``d01_设备台账_城东片区一次设备``），
  按归一化 title（去扩展名）精确匹配映射为库内 UUID；映射不齐的用例 verdict=skip（契约允许），
  detail 记缺失名单——不静默吞；
- 落库：EvaluationRun（benchmark_type='retrieval_qa'，只追加）+ 逐例 EvaluationResult
  （uk run_id+case_id）；重跑=新 run 行（基线对比经 baseline_run_id 引用，-2% 阻断口径
  08 §7.2：delta 绝对降幅超阈 → passed=False）。

纯评估面，不写任何知识产物；LLM 忠实度判定（expected_points）属 M3 评估项，本执行器只做
检索排序指标。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.kb.data.orm import Document, EvaluationResult, EvaluationRun

if TYPE_CHECKING:  # 仅类型注解（避免 business 内模块环）
    from services.kb.business.search_service import KnowledgeSearchService

logger = logging.getLogger(__name__)

DEFAULT_GOLDEN_PATH: Final = Path(__file__).resolve().parents[3] / "seeds" / "golden" / "power_retrieval_golden.jsonl"
GOLDEN_VERSION: Final = "power-golden-30@v1"
DELTA_BLOCK_THRESHOLD: Final = -0.02  # 劣化>2% 阻断（08 §7.2）
_DOC_EXTENSIONS: Final = (".md", ".txt", ".pdf", ".docx")


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """golden 用例只读快照（评估运行内不可变）。"""

    case_id: str
    mode: str
    question: str
    expected_doc_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetrievalEvalReport:
    """评估运行回执：run 行 id + 已落库的 metrics（与 EvaluationRun.metrics 同构）。"""

    run_id: uuid.UUID
    metrics: dict[str, Any]


def load_golden_cases(path: Path = DEFAULT_GOLDEN_PATH) -> list[GoldenCase]:
    """读 golden jsonl（case_id 重复即 ValueError——评估集完整性前置校验）。"""
    cases: list[GoldenCase] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            case_id = str(row["case_id"])
            if case_id in seen:
                raise ValueError(f"golden case_id 重复: {case_id}")
            seen.add(case_id)
            cases.append(
                GoldenCase(
                    case_id=case_id,
                    mode=str(row["mode"]),
                    question=str(row["question"]),
                    expected_doc_ids=tuple(str(d) for d in row.get("expected_doc_ids", [])),
                )
            )
    if not cases:
        raise ValueError(f"golden 集为空: {path}")
    return cases


def _normalize_title(title: str) -> str:
    """title → stem：去常见文档扩展名后.strip（golden 侧按 stem 引用）。"""
    lowered = (title or "").strip()
    for ext in _DOC_EXTENSIONS:
        if lowered.lower().endswith(ext):
            return lowered[: -len(ext)].strip()
    return lowered


async def _title_uuid_map(
    session: AsyncSession, *, tenant_id: uuid.UUID, kb_id: uuid.UUID | None
) -> dict[str, uuid.UUID]:
    """库内文档 title(stem) → UUID（同租户；kb_id 给定时收窄到该库）。"""
    stmt = select(Document.id, Document.title).where(Document.tenant_id == tenant_id)
    if kb_id is not None:
        stmt = stmt.where(Document.kb_collection_id == kb_id)
    rows = (await session.execute(stmt)).all()
    mapping: dict[str, uuid.UUID] = {}
    for doc_id, title in rows:
        mapping.setdefault(_normalize_title(title), doc_id)  # 重名取先（注册顺序），可复现
    return mapping


async def run_retrieval_eval(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    search: KnowledgeSearchService,
    kb_id: uuid.UUID | None = None,
    top_k: int = 8,
    golden_path: Path = DEFAULT_GOLDEN_PATH,
    baseline_run_id: uuid.UUID | None = None,
    trigger_ref: dict[str, Any] | None = None,
    started_by: uuid.UUID | None = None,
) -> RetrievalEvalReport:
    """全量 golden 评估并落库（EvaluationRun + 逐例 EvaluationResult）；返回 run 回执。"""
    cases = load_golden_cases(golden_path)
    async with session_factory() as session:
        title_map = await _title_uuid_map(session, tenant_id=tenant_id, kb_id=kb_id)
        baseline_metrics: dict[str, Any] | None = None
        if baseline_run_id is not None:
            baseline_metrics = (
                await session.execute(
                    select(EvaluationRun.metrics).where(
                        EvaluationRun.id == baseline_run_id, EvaluationRun.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if baseline_metrics is None:
                raise ValueError(f"基线 run 不存在: {baseline_run_id}")

    hit_flags: list[bool] = []
    rrs: list[float] = []
    covered: list[bool] = []
    by_mode: dict[str, dict[str, list[float]]] = {}
    case_rows: list[dict[str, Any]] = []
    for case in cases:
        expected_uuids = [title_map[stem] for stem in case.expected_doc_ids if stem in title_map]
        missing = [stem for stem in case.expected_doc_ids if stem not in title_map]
        if missing or not expected_uuids:  # 期望文档未入库 → skip（契约允许），detail 留缺失名单
            case_rows.append(
                {
                    "case": case,
                    "verdict": "skip",
                    "metrics": {"reason": "expected_docs_missing", "missing": missing},
                    "detail": {"missing": missing, "mode": case.mode},
                }
            )
            continue
        result = await search.search(
            tenant_id=tenant_id, query=case.question, kb_id=kb_id, top_k=top_k, mode=case.mode
        )
        returned = [c.doc_id for c in result.citations]
        first_rank = next(
            (rank for rank, doc_id in enumerate(returned, start=1) if doc_id in set(expected_uuids)),
            None,
        )
        hit = first_rank is not None
        rr = 1.0 / first_rank if hit else 0.0
        hit_flags.append(hit)
        rrs.append(rr)
        covered.append(bool(returned))
        bucket = by_mode.setdefault(case.mode, {"hit": [], "rr": []})
        bucket["hit"].append(1.0 if hit else 0.0)
        bucket["rr"].append(rr)
        case_rows.append(
            {
                "case": case,
                "verdict": "pass" if hit else "fail",
                "metrics": {"hit": 1.0 if hit else 0.0, "rr": round(rr, 4), "first_rank": first_rank},
                "detail": {
                    "mode": case.mode,
                    "expected": case.expected_doc_ids,
                    "returned_doc_ids": [str(d) for d in returned],
                    "n_citations": len(returned),
                    "degraded": result.degraded,
                },
            }
        )

    n = len(hit_flags)
    metrics: dict[str, Any] = {
        "golden_version": GOLDEN_VERSION,
        "top_k": top_k,
        "n_cases": len(cases),
        "n_evaluated": n,
        "n_skipped": len(cases) - n,
        "hit_at_k": round(sum(hit_flags) / n, 4) if n else None,
        "mrr": round(sum(rrs) / n, 4) if n else None,
        "citation_coverage": round(sum(covered) / n, 4) if n else None,
        "by_mode": {
            mode: {
                "n": len(bucket["hit"]),
                "hit_at_k": round(sum(bucket["hit"]) / len(bucket["hit"]), 4) if bucket["hit"] else None,
                "mrr": round(sum(bucket["rr"]) / len(bucket["rr"]), 4) if bucket["rr"] else None,
            }
            for mode, bucket in sorted(by_mode.items())
        },
    }
    if baseline_metrics and metrics["hit_at_k"] is not None:
        base_hit = baseline_metrics.get("hit_at_k")
        base_mrr = baseline_metrics.get("mrr")
        delta: dict[str, Any] = {}
        if isinstance(base_hit, (int, float)):
            delta["hit_at_k"] = round(metrics["hit_at_k"] - base_hit, 4)
        if isinstance(base_mrr, (int, float)):
            delta["mrr"] = round(metrics["mrr"] - base_mrr, 4)
        metrics["delta"] = delta
        metrics["baseline_run_id"] = str(baseline_run_id)
    passed = True
    d = metrics.get("delta") or {}
    if any(isinstance(d.get(k), (int, float)) and d[k] < DELTA_BLOCK_THRESHOLD for k in ("hit_at_k", "mrr")):
        passed = False  # 主指标劣化>2% 阻断（08 §7.2）

    async with session_factory() as session, session.begin():  # 短事务：run + 全量逐例行一次写
        run = EvaluationRun(
            tenant_id=tenant_id,
            benchmark_type="retrieval_qa",
            benchmark_version=GOLDEN_VERSION,
            trigger_ref=trigger_ref,
            baseline_run_id=baseline_run_id,
            metrics=metrics,  # JSONB 整体重赋值
            delta=metrics.get("delta"),
            passed=passed,
            started_by=started_by,
        )
        session.add(run)
        await session.flush()
        for row in case_rows:
            session.add(
                EvaluationResult(
                    tenant_id=tenant_id,
                    run_id=run.id,
                    case_id=row["case"].case_id,
                    metrics=row["metrics"],  # JSONB 整体重赋值
                    verdict=row["verdict"],
                    detail=row["detail"],
                )
            )
    logger.info(
        "retrieval_eval done: run_id=%s cases=%d hit@k=%s mrr=%s skipped=%d passed=%s",
        run.id, len(cases), metrics["hit_at_k"], metrics["mrr"], metrics["n_skipped"], passed,
    )
    return RetrievalEvalReport(run_id=run.id, metrics=metrics)
