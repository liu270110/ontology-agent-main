"""ORSI G0 缺口轨运行入口（确定性、零 LLM；architecture/09 §13.3 G0 最小可运行）。

两种模式（均走同一管线：采集 → 滑窗聚类 → 达标开单 → Markdown 报告）：

- ``--demo``：注入 **demo 标注**的合成缺口事件（6 条同指纹 execution_failure，>阈值 5；
  另 2 条低频不同指纹作阈值边界对照）——不触任何真实信号源；
- ``--live``：真实读 PG 回写台账 FAILED 行（``LedgerFailureSink``，``OA_`` 配置经
  ``Settings()``；需 PG 可达且 ``--tenant-id`` 指定租户）。

用法::

    python services/tools/orsi/run_g0.py --demo [--store-dir DIR] [--threshold 5] [--window-days 30] [--report FILE]
    python services/tools/orsi/run_g0.py --live --tenant-id <UUID> [--limit 200] [--store-dir DIR] …（同上）

报告内容：簇指纹/规模/处置、生成的 proposal id 列表、建议起草路径（G1 三级降路径提示，
本批不执行——LLM 起草是 G1 后续批次）。事件与工单留痕落 ``GapStore``（JSONL，可审计可复现）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))  # 直跑脚本（python services/tools/orsi/run_g0.py）的 services 导入位

from services.rsi.audit import LoggingAuditTrail  # noqa: E402
from services.rsi.gap import GapCollector, GapEvent, GapKind, GapStore, JsonlGapStore  # noqa: E402
from services.rsi.service import RsiService  # noqa: E402
from services.rsi.sinks import LedgerFailureSink  # noqa: E402

DEFAULT_STORE_DIR = ".orsi-g0"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ORSI G0 缺口轨最小可运行（确定性零 LLM，09 §13.3）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="注入 demo 合成事件（6+2 条，标注 demo，非真实信号）")
    mode.add_argument("--live", action="store_true", help="真实读 PG 回写台账 FAILED 行（OA_ 配置，需 PG 可达）")
    parser.add_argument("--tenant-id", type=str, default=None, help="租户 UUID（--live 必填；demo 固定演示租户）")
    parser.add_argument(
        "--store-dir", type=str, default=DEFAULT_STORE_DIR, help=f"GapStore 目录（默认 {DEFAULT_STORE_DIR}）"
    )
    parser.add_argument("--window-days", type=int, default=30, help="滑窗天数（默认 30，§13.3 G0）")
    parser.add_argument("--threshold", type=int, default=5, help="开单阈值：簇规模 ≥ threshold（默认 5，§13.3 G0）")
    parser.add_argument("--limit", type=int, default=200, help="--live 单轮拉取台账行上限（默认 200）")
    parser.add_argument("--report", type=str, default=None, help="Markdown 报告落盘路径（缺省仅打印 stdout）")
    args = parser.parse_args(argv)
    if args.live and not args.tenant_id:
        parser.error("--live 必须提供 --tenant-id（台账租户作用域）")
    if args.live:
        try:
            uuid.UUID(args.tenant_id)
        except ValueError:
            parser.error(f"--tenant-id 非法 UUID: {args.tenant_id}")
    return args


# ---------------------------------------------------------------- demo 信号（合成，标注 demo）


def _demo_events(now: datetime) -> list[GapEvent]:
    """demo 合成事件（⚠ 非真实信号）：6 条同指纹 execution_failure（>阈值 5）+ 2 条对照。

    主簇：同一行动类 + 同一失败模式（错误码归一后）→ 同指纹；对照簇：不同行动类、仅
    2 条（低于阈值 5）——展示滑窗阈值边界（不达标不开单）。
    """
    tenant = uuid.UUID("00000000-0000-0000-0000-000000000001")  # 演示租户（固定值，可复现）
    main_action = "https://onto.example/ob2/QueryPowerOutageRange"
    side_action = "https://onto.example/ob2/ExportOutageReport"
    events: list[GapEvent] = []
    for i in range(6):
        events.append(
            GapEvent(
                tenant_id=tenant,
                kind=GapKind.EXECUTION_FAILURE,
                action_iri=main_action,
                occurred_at=now - timedelta(minutes=i + 1),
                source="demo",  # ⚠ demo 合成信号（非真实运行数据）
                failure_mode="MCP_TARGET_UNAVAILABLE: 行动类未绑定连接器（demo 合成失败）",
                entity_types=("cim:OutageOrder",),
                trace_ids=(f"demo-g0-trace-{i + 1:02d}",),
                detail={"demo": True},
            )
        )
    for i in range(2):
        events.append(
            GapEvent(
                tenant_id=tenant,
                kind=GapKind.EXECUTION_FAILURE,
                action_iri=side_action,
                occurred_at=now - timedelta(minutes=i + 1),
                source="demo",  # ⚠ demo 合成信号（非真实运行数据）
                failure_mode="ADAPTER_TIMEOUT: 适配器执行超时（demo 合成失败）",
                entity_types=("cim:OutageOrder",),
                trace_ids=(f"demo-g0-trace-side-{i + 1:02d}",),
                detail={"demo": True},
            )
        )
    return events


# ---------------------------------------------------------------- 管线与报告


async def _run(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    store: GapStore = JsonlGapStore(args.store_dir)
    service = RsiService(audit_trail=LoggingAuditTrail())
    collector = GapCollector(store=store, rsi=service)

    if args.demo:
        events = _demo_events(now)
        print(f"[demo] 注入合成缺口事件 {len(events)} 条（⚠ 非真实信号；主簇 6 条同指纹 + 对照簇 2 条）")
    else:
        tenant_id = uuid.UUID(args.tenant_id)
        settings = _load_settings()
        sink = LedgerFailureSink(tenant_id=tenant_id, open_repo=_open_ledger_repo(settings, tenant_id))
        events = await sink.drain(limit=args.limit)
        print(f"[live] 台账 FAILED 行映射缺口事件 {len(events)} 条（tenant={tenant_id}，limit={args.limit}）")

    for event in events:
        await collector.collect(event)
    evaluation = await collector.evaluate(window_days=args.window_days, threshold=args.threshold, now=now)

    report = _render_report(args, now, events, evaluation)
    print(report)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(report, encoding="utf-8")
        print(f"[report] Markdown 报告已落盘: {args.report}")
    print(f"[store] 事件/工单留痕目录: {Path(args.store_dir).resolve()}")
    return 0


def _load_settings() -> Any:
    from services.platform.config import get_settings

    return get_settings()


def _open_ledger_repo(settings: Any, tenant_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """PG 台账仓储上下文**工厂**（零参可重复调用；短事务即用即弃；OA_ 配置 → async engine）。

    LedgerFailureSink.open_repo 契约是工厂（drain 内 ``async with self._open_repo()`` 每次
    调用取新上下文）——返回 ``_ctx`` 本体；返回 ``_ctx()`` 实例则上下文管理器不可再调用，
    drain 进入即 TypeError（B10 批 ocr 评审 ① 同款修复，run_g1 已同修）。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from services.writeback.data.repo_impl.writeback_repo import PgWritebackLedgerRepository

    engine = create_async_engine(settings.pg_dsn, pool_pre_ping=True)

    @asynccontextmanager
    async def _ctx():  # type: ignore[no-untyped-def]
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            yield PgWritebackLedgerRepository(db, tenant_id)

    return _ctx


def _render_report(args: argparse.Namespace, now: datetime, events: list[GapEvent], evaluation: Any) -> str:
    """Markdown 运行报告：簇指纹/规模/处置 + 开单 id 列表 + G1 三级降路径提示。"""
    mode = "demo（⚠ 合成数据，非真实信号）" if args.demo else "live（PG 回写台账 FAILED 行）"
    lines: list[str] = [
        "# ORSI G0 缺口轨运行报告",
        "",
        f"- 运行模式：**{mode}**（全确定性、零 LLM，architecture/09 §13.3 G0）",
        f"- 运行时刻：{now.isoformat(timespec='seconds')}",
        f"- 参数：window={args.window_days} 天 / threshold={args.threshold} / store={Path(args.store_dir)}",
        f"- 事件：采集 {len(events)} 条，窗口内 {evaluation.events_in_window} 条，聚类 {len(evaluation.clusters)} 簇",
        "",
        "## 簇明细（指纹前 12 位 · 规模降序）",
        "",
        "| # | 类型 | 指纹 | 规模 | 处置 | proposal |",
        "| - | ---- | ---- | ---- | ---- | -------- |",
    ]
    for idx, cluster in enumerate(evaluation.clusters, start=1):
        lines.append(
            f"| {idx} | {cluster.kind} | `{cluster.fingerprint[:12]}` | {cluster.size} "
            f"| {cluster.disposition} | {cluster.proposal_id or '—'} |"
        )
    lines += ["", "## 生成的缺口工单（proposal id 列表）", ""]
    if evaluation.opened:
        for proposal in evaluation.opened:
            evidence = proposal.envelope["gap"]["evidence"]
            lines += [
                f"- `{proposal.id}`",
                f"  - target: `{proposal.target}`（status=draft，surface=O1 工具实现，trigger=gap）",
                f"  - 证据：簇规模 {evidence['cluster_size']} / 窗口 {evidence['window_days']} 天 / "
                f"指纹 `{evidence['fingerprint'][:12]}` / 首见 {evidence['first_seen'][:19]}"
                f" 末见 {evidence['last_seen'][:19]}",
            ]
    else:
        lines.append("-（无达标簇，未开单）")
    lines += [
        "",
        "## 建议起草路径（G1 三级降路径提示——本批不执行，LLM 起草属 G1 后续批次）",
        "",
        "① **组合既有工具**（最低风险：既有行动类编排成计划模板，落 O5 面而非 O1）→",
        "② **市场能力包**检索安装（L2 通道，走既有审核）→",
        "③ **LLM 起草候选工具**（MCP server / CLI 包装 / API 调用链脚本；产物必须带行动类语义标注",
        "+ 声明 `executionMode`——无语义标注不上架）。",
        "达标簇的场景样本已存工单 evidence（G2 沉淀为黄金场景集，S2 沙箱回放 + 三级门禁）。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
