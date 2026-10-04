"""MCP 出口独立进程入口（python -m services.mcp [options]）。

形态：独立进程 MCP Server（MCP 出口形态锚点 §3.1）。能力装配自 2026-09-27 M4 验收起
与 gateway 共用**共享工厂** ``services.mcp.bootstrap.build_capability_registry``（P1-1
收口，消除「独立进程窄装」双注册表割裂）——knowledge/ontology/memory/action/
writeback.status 五 provider 全量注册：

- knowledge.search：kb 公开检索服务直连（services.kb.business.search_service=规则 3 许可面；
  豁免边随本批合入 pyproject，见 bootstrap 模块 docstring「契约需求」）；嵌入不可达自动
  BM25-only 降级（degraded=true），空库/零命中为空结果非失败；
- ontology.validate / query / consistency：制品装载器为组合根注入参数，独立进程 M4.1 未注入
  → 结构化降级 5004；classification / query 的会话工厂读模型路径可用；
- action.invoke：回写执行面（Mock 电力工单连接器 + ActionDispatcher，工厂内装配）；
- writeback.status：台账状态查询面（api/03 §3.9，同一 dispatcher 承载）。

鉴权（M4.1 + Agent13 §4 K3 双层授权）：``--anonymous-scopes`` 显式授予匿名授权集（缺省空=
deny-by-default 全拒），语义保持为 list/call 两组授权集的**默认值**；``--anonymous-list-scopes``
单独覆盖 list 可见性组（外部 tool 挂载面过滤；call 段始终由 ``--anonymous-scopes`` 承载）。
外部 tool 须显式授予其推导 scope（``external:{server}:{tool}``）方可挂载。
``--tenant-id`` 绑定匿名通道租户（缺省 NIL 租户；数据类工具须绑定真实租户方可见数据）。
审计：PG audit_logs 汇（PgInvocationAuditSink；NIL 租户/PG 不可用降级日志汇，不阻塞）。
OAuth 2.1 / API Key 通道随供给篇 C4（M5+）替换。

用法：
    python -m services.mcp --transport stdio                      # 本地插件接入
    python -m services.mcp --transport http --host 0.0.0.0 --port 9800 \
        --tenant-id <uuid> \
        --targets services/mcp/targets.example.json \
        --anonymous-scopes "kb:read,ontology:read,memory:read,memory:write,action:invoke"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from services.mcp.bootstrap import PgInvocationAuditSink, build_capability_registry
from services.mcp.client import ExternalMcpManager, load_targets
from services.mcp.registry import CapabilityRegistry
from services.mcp.server import build_mcp_server


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m services.mcp", description="ontology-agent MCP 出口")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio", help="传输（默认 stdio）")
    parser.add_argument("--host", default="127.0.0.1", help="http 监听地址")
    parser.add_argument("--port", type=int, default=9800, help="http 监听端口")
    parser.add_argument("--targets", default=None, help="外部 MCP 目标配置文件（JSON；缺省不接入外部 server）")
    parser.add_argument(
        "--anonymous-scopes",
        default="",
        help="逗号分隔的匿名授权 scope 集（缺省空=全拒；call 段权威，list 段缺省同值；M5 换 OAuth/API Key 通道）",
    )
    parser.add_argument(
        "--anonymous-list-scopes",
        default=None,
        help="list 可见性授权集（逗号分隔；K3 双层授权第一段覆盖位，缺省与 --anonymous-scopes 同值）",
    )
    parser.add_argument(
        "--tenant-id",
        type=uuid.UUID,
        default=None,
        help="匿名通道绑定租户（缺省 NIL 租户=无数据可见；api/03 §6 租户隔离）",
    )
    parser.add_argument("--no-external", action="store_true", help="不把外部 server tool 挂载进出口")
    return parser.parse_args(argv)


async def _bootstrap(args: argparse.Namespace, registry: CapabilityRegistry) -> ExternalMcpManager | None:
    """外部接入引导：加载目标配置 → 发现 tools/list → 命名空间隔离登记（失败 fail-soft 记日志）。"""
    if not args.targets:
        return None
    import logging

    logger = logging.getLogger("services.mcp.main")
    try:
        manager = ExternalMcpManager(load_targets(args.targets))
        if len(manager.connectors) > 0:
            registered = await manager.refresh(registry)
            logger.info("外部 MCP tool 登记完成: %d 项", len(registered))
        return manager
    except Exception:  # noqa: BLE001 ——外部接入失败不阻塞出口启动（MCP 篇 §4 失败隔离）
        logger.exception("外部 MCP 接入失败（出口以无外部 tool 继续）: targets=%s", args.targets)
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if sys.platform == "win32":
        # psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用，同测试夹具纪律）
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    from services.platform.config import get_settings
    from services.platform.deps import get_session_factory

    settings = get_settings()
    session_factory = get_session_factory(settings)
    # 共享工厂装配（P1-1 收口）：五 provider 全量；制品装载器独立进程未注入（5004 降级见模块 docstring）
    registry, _dispatcher = build_capability_registry(session_factory, settings=settings)
    manager = asyncio.run(_bootstrap(args, registry))

    scopes = tuple(s.strip() for s in args.anonymous_scopes.split(",") if s.strip())
    list_scopes = (
        None
        if args.anonymous_list_scopes is None
        else tuple(s.strip() for s in args.anonymous_list_scopes.split(",") if s.strip())
    )
    mcp = build_mcp_server(
        registry,
        audit_sink=PgInvocationAuditSink(session_factory),
        granted_scopes=scopes,
        list_granted_scopes=list_scopes,
        include_external=not args.no_external,
        default_tenant_id=args.tenant_id,
    )

    try:
        if args.transport == "stdio":
            mcp.run(transport="stdio")
        else:
            mcp.run(transport="http", host=args.host, port=args.port)
    finally:
        if manager is not None:
            asyncio.run(manager.close())
    return 0


if __name__ == "__main__":
    sys.exit(main())
