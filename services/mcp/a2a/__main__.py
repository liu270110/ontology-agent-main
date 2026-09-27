"""A2A 独立进程入口（python -m services.mcp.a2a [options]；形态同 services/mcp/__main__ 模式）。

形态：独立监听 HTTP（api/04 §7「同进程 or 独立监听」随 M5 裁决，本批独立监听起步）；
装配：platform UoW（Task 受理路径，规则 3）+ services.mcp.bootstrap.PgInvocationAuditSink
（委托/查询/取消全留痕；NIL 租户/PG 不可用降级日志汇）+ ApiKeyAuthorizer（API Key 起步，
api/04 §5——OAuth 2.0/OIDC 委托随 M5+ 通道替换）。

鉴权模型（deny-by-default）：``--api-key`` 可重复授予（每 key 绑定 ``--granted-scopes``
授权集，缺省空=全部请求 2001 拒绝）；``--tenant-id`` 绑定委托受理租户（必填——task 行
FK 归租户，缺省不提供 NIL 兜底，防止误入无租户数据面）。

用法：
    python -m services.mcp.a2a --host 127.0.0.1 --port 9801 \\
        --tenant-id <uuid> \\
        --api-key <key1> --api-key <key2> \\
        --granted-scopes "session:read,session:write,session:chat" \\
        --skills-file skills.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("services.mcp.a2a.main")

# 独立入口缺省技能占位（如实标注占位语义——skills 与本体行动类同源映射随 changeset
# publish 流水线接入，api/04 §3；不冒充本体行动类）
_DEFAULT_SKILL = {
    "id": "task-delegate",
    "name": "平台任务委托",
    "description": "把一个任务整体委托给平台执行（知识检索/本体推理/业务行动闭环）；"
    "占位条目——skills 与本体行动类同源映射随本体发布流水线接入（api/04 §3）",
    "tags": ["platform"],
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m services.mcp.a2a", description="ontology-agent A2A v1.0 出口")
    parser.add_argument("--host", default="127.0.0.1", help="http 监听地址")
    parser.add_argument("--port", type=int, default=9801, help="http 监听端口")
    parser.add_argument("--tenant-id", type=uuid.UUID, required=True, help="委托受理绑定租户（必填，task FK 归宿）")
    parser.add_argument("--api-key", action="append", default=[], dest="api_keys", help="委托方 API Key（可重复）")
    parser.add_argument(
        "--granted-scopes",
        default="",
        help="逗号分隔的委托方 scope 集（缺省空=deny-by-default 全拒；api/01 §5.2 登记集）",
    )
    parser.add_argument("--skills-file", default=None, help="Agent Card skills 清单 JSON 文件（缺省用占位技能）")
    parser.add_argument("--base-url", default=None, help="Card url 前缀（缺省 http://<host>:<port>）")
    parser.add_argument("--timeout-s", type=float, default=15.0, help="单请求处理超时（秒）")
    return parser.parse_args(argv)


def _load_skills(path: str | None) -> list[dict[str, Any]]:
    """skills 装配：--skills-file JSON 清单优先，缺省用平台委托占位技能。"""
    if not path:
        return [dict(_DEFAULT_SKILL)]
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not all(isinstance(row, dict) and row.get("id") for row in rows):
        raise ValueError("skills-file 须为 [{id, name, description, tags}] 清单")
    return rows


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if sys.platform == "win32":
        # psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用，同 services.mcp.__main__ 纪律）
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from services.mcp.a2a.app import build_a2a_app
    from services.mcp.a2a.auth import ApiKeyAuthorizer
    from services.mcp.a2a.card import AgentSkill, build_agent_card
    from services.mcp.a2a.service import A2aService
    from services.mcp.bootstrap import PgInvocationAuditSink
    from services.platform.config import get_settings
    from services.platform.db.uow import AsyncUnitOfWork
    from services.platform.deps import get_session_factory

    settings = get_settings()
    session_factory = get_session_factory(settings)
    skills = [AgentSkill(**row) for row in _load_skills(args.skills_file)]
    base_url = args.base_url or f"http://{args.host}:{args.port}"
    card = build_agent_card(base_url=base_url, skills=skills)
    scopes = tuple(s.strip() for s in args.granted_scopes.split(",") if s.strip())
    service = A2aService(
        uow=AsyncUnitOfWork(session_factory),
        tenant_id=args.tenant_id,
        audit_sink=PgInvocationAuditSink(session_factory),
    )
    if not args.api_keys:
        logger.warning("未授予任何 --api-key：全部委托请求将被 401 拒绝（deny-by-default）")
    app = build_a2a_app(
        card=card,
        service=service,
        authorizer=ApiKeyAuthorizer({key: scopes for key in args.api_keys}),
        timeout_s=args.timeout_s,
    )

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
