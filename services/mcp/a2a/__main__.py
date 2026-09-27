"""A2A 独立进程入口（python -m services.mcp.a2a [options]；形态同 services/mcp/__main__ 模式）。

形态：独立监听 HTTP（api/04 §7「同进程 or 独立监听」随 M5 裁决，本批独立监听起步）；
装配（M5 登记缓议项：委托执行通路接线）：
- platform UoW（Task 受理 + 会话/任务终态路径，规则 3）+ services.mcp.bootstrap.
  PgInvocationAuditSink（委托/查询/取消/执行全留痕；PG 不可用降级日志汇）；
- ApiKeyAuthorizer（API Key 起步，api/04 §5——OAuth 2.0/OIDC 委托随 M5+ 通道替换）；
- **chat 编排链**（api/04 §6「与 REST 发消息同一条编排链，不另建执行通路」）：chat 依赖经
  公开面装配——记忆 L1 经 memory.business.runtime.build_l1_store、检索经 chat_context
  工厂内置的 kb.business.search_service（build_chat_orchestrator 内收敛）、结果汇经
  agent.api.sessions.build_chat_result_sink（REST 同款 PG 短事务）；模型端口与 gateway
  组合根同款（OpenAI 兼容直驱 + 审计/预算包裹，_build_model_port 同参复制）；
- A2aTaskExecutor（services/mcp/a2a/executor.py）：受理后台执行 → 终态落库 + artifacts。

鉴权模型（deny-by-default）：``--api-key`` 可重复授予（每 key 绑定 ``--granted-scopes``
授权集，缺省空=全部请求 2001 拒绝）；``--tenant-id`` 绑定委托受理租户（必填——task 行
FK 归租户）；``--agent-id`` 绑定委托会话归属 agent（必填——会话聚合不变式要求，disabled
在执行期拒绝）。

用法：
    python -m services.mcp.a2a --host 127.0.0.1 --port 9801 \\
        --tenant-id <uuid> --agent-id <uuid> \\
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

# 模型端口装配常量（与 gateway 组合根同参复制；随 config 收口批次统一进 Settings）
_LLM_TIMEOUT_S = 60.0
_LLM_AUDIT_MAX_BATCH = 100
_LLM_AUDIT_FLUSH_INTERVAL_S = 1.0
_LLM_BUDGET_WINDOW_TOKENS = 500_000
_LLM_BUDGET_WINDOW_S = 3600


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m services.mcp.a2a", description="ontology-agent A2A v1.0 出口")
    parser.add_argument("--host", default="127.0.0.1", help="http 监听地址")
    parser.add_argument("--port", type=int, default=9801, help="http 监听端口")
    parser.add_argument("--tenant-id", type=uuid.UUID, required=True, help="委托受理绑定租户（必填，task FK 归宿）")
    parser.add_argument(
        "--agent-id", type=uuid.UUID, required=True, help="委托会话归属 agent（必填；disabled 执行期拒绝）"
    )
    parser.add_argument("--api-key", action="append", default=[], dest="api_keys", help="委托方 API Key（可重复）")
    parser.add_argument(
        "--granted-scopes",
        default="",
        help="逗号分隔的委托方 scope 集（缺省空=deny-by-default 全拒；api/01 §5.2 登记集）",
    )
    parser.add_argument("--skills-file", default=None, help="Agent Card skills 清单 JSON 文件（缺省用占位技能）")
    parser.add_argument("--base-url", default=None, help="Card url 前缀（缺省 http://<host>:<port>）")
    parser.add_argument("--timeout-s", type=float, default=15.0, help="单请求处理超时（秒）")
    parser.add_argument(
        "--chat-timeout-s",
        type=float,
        default=None,
        help="单委托执行硬超时秒（缺省=chat 总预算 60s + 5s 收尾余量）",
    )
    return parser.parse_args(argv)


def _load_skills(path: str | None) -> list[dict[str, Any]]:
    """skills 装配：--skills-file JSON 清单优先，缺省用平台委托占位技能。"""
    if not path:
        return [dict(_DEFAULT_SKILL)]
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not all(isinstance(row, dict) and row.get("id") for row in rows):
        raise ValueError("skills-file 须为 [{id, name, description, tags}] 清单")
    return rows


def _build_model_port(s: Any) -> Any:
    """模型端口装配（与 gateway 组合根同款：OpenAI 兼容直驱 + 审计/预算包裹；14 篇 §9）。

    无 llm 配置 → None（builtin 不注册，委托对话以 5002 LLM_UNAVAILABLE 落 failed 终态，
    受理/查询/取消面不受影响）。
    """
    if not (s.llm_base_url and s.llm_api_key):
        return None
    from services.platform.deps import get_redis, get_session_factory
    from services.platform.llm.audit import LlmCallAuditBuffer
    from services.platform.llm.audited import AuditedModelPort
    from services.platform.llm.budget import LlmBudgetGate
    from services.platform.llm.gateway import OpenAICompatibleModelPort

    inner = OpenAICompatibleModelPort(
        base_url=s.llm_base_url, api_key=s.llm_api_key, model=s.llm_model, timeout_s=_LLM_TIMEOUT_S
    )
    audit = LlmCallAuditBuffer(
        get_session_factory(s), max_batch=_LLM_AUDIT_MAX_BATCH, flush_interval_s=_LLM_AUDIT_FLUSH_INTERVAL_S
    )
    budget = LlmBudgetGate(get_redis(s), limit_tokens=_LLM_BUDGET_WINDOW_TOKENS, window_s=_LLM_BUDGET_WINDOW_S)
    return AuditedModelPort(inner, audit, budget)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if sys.platform == "win32":
        # psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用，同 services.mcp.__main__ 纪律）
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from services.agent.api.sessions import build_chat_result_sink  # agent 公开面（api，规则 3）
    from services.agent.business.chat_orchestrator import build_chat_orchestrator  # agent 公开面（business，规则 3）
    from services.mcp.a2a.app import build_a2a_app
    from services.mcp.a2a.auth import ApiKeyAuthorizer
    from services.mcp.a2a.card import AgentSkill, build_agent_card
    from services.mcp.a2a.executor import InMemoryA2aResultStore, build_a2a_task_executor
    from services.mcp.a2a.service import A2aService
    from services.mcp.bootstrap import PgInvocationAuditSink
    from services.memory.business.runtime import build_l1_store  # memory 公开装配面（规则 3；memory.data 私有）
    from services.platform.config import get_settings
    from services.platform.db.uow import AsyncUnitOfWork
    from services.platform.deps import get_redis, get_session_factory

    settings = get_settings()
    session_factory = get_session_factory(settings)
    uow = AsyncUnitOfWork(session_factory)
    skills = [AgentSkill(**row) for row in _load_skills(args.skills_file)]
    base_url = args.base_url or f"http://{args.host}:{args.port}"
    card = build_agent_card(base_url=base_url, skills=skills)
    scopes = tuple(s.strip() for s in args.granted_scopes.split(",") if s.strip())

    # chat 编排链装配（api/04 §6：与 REST 发消息同链路；result_sink=REST 同款 PG 结果汇）
    orchestrator = build_chat_orchestrator(
        model_port=_build_model_port(settings),
        l1_store=build_l1_store(get_redis(settings), ttl_seconds=settings.memory_l1_ttl_seconds),
        session_factory=session_factory,
        ollama_base_url=settings.ollama_base_url,
        result_sink=build_chat_result_sink(uow),
    )
    result_store = InMemoryA2aResultStore()
    executor_kwargs: dict[str, Any] = {}
    if args.chat_timeout_s is not None:
        executor_kwargs["chat_timeout_s"] = args.chat_timeout_s
    executor = build_a2a_task_executor(
        uow=uow,
        tenant_id=args.tenant_id,
        agent_id=args.agent_id,
        orchestrator=orchestrator,
        audit_sink=PgInvocationAuditSink(session_factory),
        result_store=result_store,
        **executor_kwargs,
    )

    service = A2aService(uow=uow, tenant_id=args.tenant_id, audit_sink=PgInvocationAuditSink(session_factory))
    if not args.api_keys:
        logger.warning("未授予任何 --api-key：全部委托请求将被 401 拒绝（deny-by-default）")
    app = build_a2a_app(
        card=card,
        service=service,
        authorizer=ApiKeyAuthorizer({key: scopes for key in args.api_keys}),
        timeout_s=args.timeout_s,
        executor=executor,
        result_store=result_store,
        cancel_hook=executor.cancel,
    )

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
