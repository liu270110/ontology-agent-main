"""ORSI G1 起草运行入口（确定性优先 → agent，三级降路径；architecture/09 §13.3 G1）。

两种模式（同一管线：G0 开缺口工单 → 逐单三级起草（L1 组合 / L2 市场 / L3 LLM）→ Markdown 报告）：

- ``--demo``：注入 **demo 标注**的合成缺口事件（两簇各 6 条：种子域内 unbound_action 簇 →
  L1 组合命中；跨域 execution_failure 簇 → L1 miss 降 L2，默认注入 **demo 合成市场**
  （⚠ 非真实市场数据，``--no-demo-market`` 关闭）演示 ② 通道命中；model 缺省 None——
  L3 整级跳过并如实标注，LLM 环节单测/--live 覆盖）；
- ``--live``：骨架实装——读 OA_ 配置真库（``Settings()`` 构造 OpenAI 兼容模型渠道 +
  台账 FAILED 行缺口事件），真跑属部署面（PG 市场仓储/真实连接器注册表注入随部署装配）。

store 目录口径（与 run_g0 共享约定）：两工具走**同一 GapStore JSONL 格式**（gap_events /
gap_proposals 两文件，追加写可审计可复现）；G0 缺省目录 ``.orsi-g0``、G1 缺省 ``.orsi-g1``
（demo 自含重放，避免跨进程池语义下重复开单去重误伤）；``--store-dir .orsi-g0`` 可指向
G0 留痕目录复用其事件/工单文件（同指纹未闭合工单仍按去重口径不重复开）。

用法::

    python services/devtools/orsi/run_g1.py --demo [--store-dir DIR] [--report FILE] [--no-demo-market]
    python services/devtools/orsi/run_g1.py --live --tenant-id <UUID> [--limit 200] [--store-dir DIR] …

红线：G1 只写 ``envelope["draft_artifact"]`` 不迁状态（全部工单保持 draft；G2 门禁/G3
转正本批不做）；L2 只产候选包引用（安装走既有审核）；L3 零幻觉上架（确定性校验 +
重试预算硬上限，越界即拒）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))  # 直跑脚本（python services/devtools/orsi/run_g1.py）的 services 导入位

from services.rsi.audit import LoggingAuditTrail  # noqa: E402
from services.rsi.drafter import (  # noqa: E402
    DraftArtifact,
    DraftAttempt,
    draft_gap_proposal_detailed,
    load_seed_actions,
)
from services.rsi.gap import GapCollector, GapEvent, GapKind, GapStore, JsonlGapStore  # noqa: E402
from services.rsi.service import RsiService  # noqa: E402
from services.rsi.sinks import LedgerFailureSink  # noqa: E402

DEFAULT_STORE_DIR = ".orsi-g1"

# 种子域 demo 簇行动类（命名空间对齐 services/seeds/power_seed.ttl 的 pw:，L1 同域组合可命中）
DEMO_SEED_DOMAIN_ACTION = "http://ontology-agent.local/o/t1/power#ComposeOutageReport"
# 跨域 demo 簇行动类（run_g0 --demo 主簇同款；L1 无同域候选 → 降 L2 演示 ② 通道）
DEMO_CROSS_DOMAIN_ACTION = "https://onto.example/ob2/QueryPowerOutageRange"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ORSI G1 起草运行入口（三级降路径，09 §13.3）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--demo", action="store_true", help="注入 demo 合成缺口事件（两簇各 6 条，标注 demo，非真实信号）"
    )
    mode.add_argument("--live", action="store_true", help="读 OA_ 配置真库（骨架实装；真跑属部署面）")
    parser.add_argument(
        "--tenant-id", type=str, default=None, help="租户 UUID（--live 必填；demo 固定演示租户）"
    )
    parser.add_argument(
        "--store-dir",
        type=str,
        default=DEFAULT_STORE_DIR,
        help=f"GapStore 目录（默认 {DEFAULT_STORE_DIR}；共享口径见模块 docstring）",
    )
    parser.add_argument(
        "--seed", type=str, default=None, help="种子本体 Turtle 路径（缺省 services/seeds/power_seed.ttl）"
    )
    parser.add_argument("--window-days", type=int, default=30, help="滑窗天数（默认 30，§13.3 G0）")
    parser.add_argument("--threshold", type=int, default=5, help="开单阈值：簇规模 ≥ threshold（默认 5，§13.3 G0）")
    parser.add_argument("--limit", type=int, default=200, help="--live 单轮拉取台账行上限（默认 200）")
    parser.add_argument(
        "--no-demo-market",
        action="store_true",
        help="--demo 不注入合成市场（演示 L1→L2→L3 全 miss 如实降级）",
    )
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


# ---------------------------------------------------------------- demo 信号与合成市场（标注 demo）


def _demo_events(now: datetime) -> list[GapEvent]:
    """demo 合成事件（⚠ 非真实信号）：两簇各 6 条（>阈值 5），演示三级降路径两型命中。

    主簇 A：种子域（pw: 命名空间）unbound_action——L1 有同域既有行动类可组合（落 O5）；
    主簇 B：跨域 execution_failure（run_g0 --demo 主簇同款行动类）——L1 无同域候选降 L2
    （默认由 demo 合成市场命中；--no-demo-market 时继续降 L3，model=None 即整链如实降级）。
    """
    tenant = uuid.UUID("00000000-0000-0000-0000-000000000001")  # 演示租户（固定值，可复现）
    events: list[GapEvent] = []
    for i in range(6):
        events.append(
            GapEvent(
                tenant_id=tenant,
                kind=GapKind.UNBOUND_ACTION,
                action_iri=DEMO_SEED_DOMAIN_ACTION,
                occurred_at=now - timedelta(minutes=i + 1),
                source="demo-g1",  # ⚠ demo 合成信号（非真实运行数据）
                entity_types=("cim:OutageOrder",),
                trace_ids=(f"demo-g1-trace-a-{i + 1:02d}",),
                detail={"demo": True},
            )
        )
    for i in range(6):
        events.append(
            GapEvent(
                tenant_id=tenant,
                kind=GapKind.EXECUTION_FAILURE,
                action_iri=DEMO_CROSS_DOMAIN_ACTION,
                occurred_at=now - timedelta(minutes=i + 1),
                source="demo-g1",  # ⚠ demo 合成信号（非真实运行数据）
                failure_mode="MCP_TARGET_UNAVAILABLE: 行动类未绑定连接器（demo 合成失败）",
                entity_types=("cim:OutageOrder",),
                trace_ids=(f"demo-g1-trace-b-{i + 1:02d}",),
                detail={"demo": True},
            )
        )
    return events


@dataclass(slots=True)
class _DemoMarketPlugin:
    """demo 合成市场插件（duck-typed Plugin 子集；⚠ 非真实市场数据）。"""

    id: uuid.UUID
    slug: str
    name: str
    kind: str
    status: str = "published"


@dataclass(slots=True)
class _DemoMarketVersion:
    """demo 合成市场版本（duck-typed PluginVersion 子集）。"""

    version: str
    status: str = "published"
    server_json: dict[str, Any] = field(default_factory=dict)


class DemoMarket:
    """demo 合成市场（⚠ 非真实市场数据）：单件已发布能力包，工具语义标注=B 簇行动类。

    只实现市场**只读检索面**（list_market/get_detail 结构子集）——plugin 侧只读不装
    （安装走既有审核），L2 检索命中即返候选包引用。
    """

    def __init__(self) -> None:
        self._plugin = _DemoMarketPlugin(
            id=uuid.UUID("00000000-0000-0000-0000-0000000000c1"),
            slug="power-outage-query",
            name="停电范围查询能力包（demo）",
            kind="mcp_server",
        )
        self._version = _DemoMarketVersion(
            version="1.0.0",
            server_json={
                "name": self._plugin.slug,
                "display_name": self._plugin.name,
                "version": "1.0.0",
                "description": "停电范围查询 MCP 能力包（demo 合成，非真实上架件）",
                "x-platform": {
                    "schema_version": 1,
                    "tools": [
                        {
                            "name": "query_outage_range",
                            "description": "按馈线/时段查询停电范围（demo 合成工具）",
                            "semantic_annotation": {"action_iri": DEMO_CROSS_DOMAIN_ACTION},
                        }
                    ],
                },
            },
        )

    async def list_market(
        self, *, status: Any = None, offset: int = 0, limit: int = 20
    ) -> list[_DemoMarketPlugin]:
        _ = status
        return [self._plugin][offset : offset + limit]

    async def get_detail(self, plugin_id: uuid.UUID) -> tuple[_DemoMarketPlugin, list[_DemoMarketVersion]]:
        if plugin_id != self._plugin.id:
            raise LookupError(f"demo 市场无此插件: {plugin_id}")
        return self._plugin, [self._version]


# ---------------------------------------------------------------- live 骨架（OA_ 配置渠道）


def _live_model(now_note: list[str]):  # type: ignore[no-untyped-def]
    """--live 模型渠道构造（工具层允许读 OA_ 环境变量；平台代码禁）。

    渠道缺配（llm_base_url/llm_api_key 任一缺失）→ None（L3 如实跳过并标注）；
    真跑属部署面（14 篇 §9 网关装配），本入口只做最小直连构造。
    """
    from services.platform.config import get_settings
    from services.platform.llm.gateway import OpenAICompatibleModelPort

    settings = get_settings()
    if not (settings.llm_base_url and settings.llm_api_key):
        now_note.append("OA_ 模型渠道未配置（llm_base_url/llm_api_key 缺失）——L3 整级跳过")
        return None
    now_note.append(f"模型渠道: {settings.llm_base_url} / {settings.llm_model}")
    return OpenAICompatibleModelPort(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_s=settings.llm_timeout_seconds,
    )


def _open_ledger_repo(settings: Any, tenant_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """PG 台账仓储上下文**工厂**（零参可重复调用；短事务即用即弃；OA_ 配置 → async engine）。

    LedgerFailureSink.open_repo 契约是工厂（drain 内 ``async with self._open_repo()`` 每次
    调用取新上下文）——必须返回 ``_ctx`` 本体，不可返回 ``_ctx()`` 实例（上下文管理器实例
    不可再调用，drain 二次进入即 TypeError）。
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


# ---------------------------------------------------------------- 管线与报告


async def _run(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    store: GapStore = JsonlGapStore(args.store_dir)
    service = RsiService(audit_trail=LoggingAuditTrail())
    collector = GapCollector(store=store, rsi=service)
    notes: list[str] = []

    if args.demo:
        events = _demo_events(now)
        market: Any = None if args.no_demo_market else DemoMarket()
        model: Any = None  # demo 缺省无模型渠道——L3 整级跳过并如实标注
        notes.append("model 未注入（demo 缺省）——L3 整级跳过，LLM 环节由单测/--live 覆盖")
        if market is not None:
            notes.append("market=demo 合成市场（⚠ 非真实市场数据；--no-demo-market 关闭）")
        print(
            f"[demo] 注入合成缺口事件 {len(events)} 条"
            "（⚠ 非真实信号；两簇各 6 条：种子域 unbound_action + 跨域 execution_failure）"
        )
    else:
        tenant_id = uuid.UUID(args.tenant_id)
        from services.platform.config import get_settings

        settings = get_settings()
        model = _live_model(notes)
        market = None  # PG 市场仓储接线随部署面（lifecycle 组合根装配）；live 骨架只读台账信号源
        sink = LedgerFailureSink(tenant_id=tenant_id, open_repo=_open_ledger_repo(settings, tenant_id))
        events = await sink.drain(limit=args.limit)
        print(f"[live] 台账 FAILED 行映射缺口事件 {len(events)} 条（tenant={tenant_id}，limit={args.limit}）")

    for event in events:
        await collector.collect(event)
    evaluation = await collector.evaluate(window_days=args.window_days, threshold=args.threshold, now=now)

    # G1 逐单三级起草（draft 工单；命中即写 envelope["draft_artifact"]，状态保持 draft）
    seed_actions = load_seed_actions(args.seed) if args.seed else load_seed_actions()
    registry_iris: frozenset[str] = frozenset()  # 真实 ConnectorRegistry 注入随部署装配（demo/live 均如实空集）
    drafted: list[tuple[Any, DraftArtifact | None, list[DraftAttempt]]] = []
    for proposal in evaluation.opened:
        artifact, attempts = await draft_gap_proposal_detailed(
            proposal,
            seed_actions=seed_actions,
            registry_iris=registry_iris,
            market=market,
            model=model,
            now=now,
        )
        drafted.append((proposal, artifact, attempts))

    report = _render_report(args, now, notes, evaluation, drafted, len(seed_actions))
    print(report)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(report, encoding="utf-8")
        print(f"[report] Markdown 报告已落盘: {args.report}")
    print(f"[store] 事件/工单留痕目录: {Path(args.store_dir).resolve()}")
    return 0


def _local_name(iri: str) -> str:
    """IRI 本地名（报告展示位：fragment 或路径末段）。"""
    return iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _artifact_summary(artifact: DraftArtifact) -> str:
    """产物摘要（报告行用：按命中路径取要点）。"""
    if artifact.path.value == "combination":
        plan = artifact.content["plan_template"]
        steps = " → ".join(_local_name(step["action_iri"]) for step in plan["steps"])
        return f"计划模板 {plan['step_count']} 步（已绑定 {plan['bound_count']}）: {steps}"
    if artifact.path.value == "market":
        ref = artifact.content["market_ref"]
        return f"市场包 {ref['slug']}@{ref['version']}（工具 {ref['matched_tool']}，match={ref['match']}）"
    return f"LLM 草案: {artifact.content.get('description', '')}（execution_mode={artifact.execution_mode}）"


def _render_report(
    args: argparse.Namespace,
    now: datetime,
    notes: list[str],
    evaluation: Any,
    drafted: list[tuple[Any, DraftArtifact | None, list[DraftAttempt]]],
    seed_action_count: int,
) -> str:
    mode = "demo（⚠ 合成数据，非真实信号）" if args.demo else "live（OA_ 配置真库，骨架实装）"
    lines: list[str] = [
        "# ORSI G1 起草运行报告",
        "",
        f"- 运行模式：**{mode}**（确定性优先 → agent 三级降路径，architecture/09 §13.3 G1）",
        f"- 运行时刻：{now.isoformat(timespec='seconds')}",
        f"- 参数：store={Path(args.store_dir)} / seed 行动类 {seed_action_count} 个 / "
        f"window={args.window_days} 天 / threshold={args.threshold}",
    ]
    lines += [f"- {note}" for note in notes]
    lines += [
        f"- 工单：本轮开单 {len(evaluation.opened)} 件（同指纹未闭合去重；逐单起草如下）",
        "",
        "## 起草明细（指纹前 12 位）",
        "",
        "| # | 指纹 | 簇规模 | 缺口型 | 行动类 | 命中路径 | surface | 结果摘要 |",
        "| - | ---- | ---- | ---- | ---- | ---- | ---- | ---- |",
    ]
    for idx, (proposal, artifact, attempts) in enumerate(drafted, start=1):
        gap = proposal.envelope["gap"]
        evidence = gap["evidence"]
        action_iri = (evidence.get("samples") or [{}])[0].get("primary_key", "—")
        if artifact is not None:
            ordinal = {"combination": "①组合", "market": "②市场", "llm": "③LLM"}[artifact.path.value]
            path_label = f"{artifact.path.value}（{ordinal}）"
            surface_label = artifact.surface.value
            summary = _artifact_summary(artifact)
        else:
            chain = " → ".join(
                f"{a.level}:{a.outcome}" for a in attempts if a.outcome not in {"hit"}
            ) or "—"
            path_label = f"未命中（{chain}）"
            surface_label = "—"
            summary = "三级均未产出草案（工单保持 draft，如实降级）"
        lines.append(
            f"| {idx} | `{evidence['fingerprint'][:12]}` | {evidence['cluster_size']} | {gap['kind']} "
            f"| `{_local_name(str(action_iri))[:44]}` | {path_label} | {surface_label} | {summary} |"
        )

    lines += ["", "## 逐级尝试留痕（降路径可审计）", ""]
    for idx, (proposal, artifact, attempts) in enumerate(drafted, start=1):
        gap = proposal.envelope["gap"]
        evidence = gap["evidence"]
        lines.append(f"- 工单 {idx}（`{proposal.id}`，指纹 `{evidence['fingerprint'][:12]}`）：")
        for attempt in attempts:
            lines.append(f"  - {attempt.level} [{attempt.outcome}] {attempt.detail}")
        if artifact is not None:
            lines.append(
                f"  - 产物已写回 `envelope['draft_artifact']`（path={artifact.path.value}，"
                f"surface={artifact.surface.value}，confidence={artifact.confidence}）"
            )
    if not drafted:
        lines.append("-（本轮无新开工单：同指纹未闭合去重或无达标簇——见 G0 簇口径）")

    lines += [
        "",
        "## 生命周期与红线",
        "",
        "- 全部工单 status **保持 draft**：G1 只写 `envelope['draft_artifact']`，候选状态迁移唯一入口在",
        "  RsiService/Proposal.transition（evaluating 起属 G2 测试批次，本批不做不 stub）；",
        "- L2 只产候选包引用：安装/启用走既有市场审核与两级验签链（plugin 侧只读不装）；",
        "- L3 零幻觉上架：产物过确定性校验（action_iri 落种子行动类集 / execution_mode 枚举 /",
        "  description 非空），越界即拒，重试预算硬上限（1+2 次），耗尽=该工单本轮起草失败；",
        "- 分界铁律（§13.2）：L1 组合落 O5 控制流程；L2 市场/L3 LLM 落 O1 工具实现，不触行动类语义。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
