"""平台 CLI 命令树装配与实现（设计权威=docs/cli/CLI设计.md；端点权威=docs/api/01；SSE=api/02）。

取舍（2026-09-28 实施裁决，报告挂账）：
- 命令框架：typer 不在依赖表（pyproject 当期禁改）→ 标准库 argparse；同步 httpx（终端形态
  无需事件循环，SSE 经 httpx.Client.stream 同步迭代）；
- 退出码（CLI设计 §6）：0 成功 / 1 通用 / 2 校验失败（400/422 与 validate 不通过）/
  3 网络（连不上/超时/4301 回放过期，附重连指引）/ 4 鉴权（401）/ 5 不存在（404）/
  6 权限（403）；
- 安全：密码走 getpass 或 OA_CLI_PASSWORD（CI），不设 --token/--password 参数（令牌不进
  进程列表）；错误输出只含 code/message/trace_id，永不回显令牌。
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote

import httpx

from services.cli.client import ApiClient, ApiError
from services.cli.config import ENV_PASSWORD, CliConfig, load_config, save_tokens
from services.cli.sse import TrunkEventRenderer, truncate

PROG = "onto"

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_VALIDATION = 2
EXIT_NETWORK = 3
EXIT_AUTH = 4
EXIT_NOT_FOUND = 5
EXIT_FORBIDDEN = 6


@dataclass
class Context:
    """命令运行上下文：配置 + 客户端 + 输出流（测试可注入）。"""

    config: CliConfig
    client: ApiClient
    stdout: TextIO
    stderr: TextIO


# ================================================================ 错误透出与退出码


def api_exit_code(exc: ApiError) -> int:
    """ApiError → 退出码（CLI设计 §6）；4301 回放过期归网络族（流连接类失败）。"""
    if exc.is_network_error or exc.code == 4301 or exc.http_status == 410:
        return EXIT_NETWORK
    if exc.http_status == 401:
        return EXIT_AUTH
    if exc.http_status == 403:
        return EXIT_FORBIDDEN
    if exc.http_status == 404:
        return EXIT_NOT_FOUND
    if exc.http_status in (400, 422):
        return EXIT_VALIDATION
    return EXIT_GENERIC


def format_api_error(exc: ApiError) -> str:
    """错误体 code/message/trace_id 透出为一行（detail 截断；永不含令牌）。"""
    code = str(exc.code) if exc.code is not None else "-"
    http_status = str(exc.http_status) if exc.http_status else "-"
    line = f"[ERR] code={code} http={http_status} {exc}"
    if exc.trace_id:
        line += f" trace_id={exc.trace_id}"
    if exc.detail is not None:
        line += f" detail={truncate(json.dumps(exc.detail, ensure_ascii=False))}"
    return line


def report_api_error(exc: ApiError, err: TextIO) -> int:
    """错误写 stderr（含 4301 专述重连指引，api/02 §4）并返回退出码。"""
    err.write(format_api_error(exc) + "\n")
    if exc.code == 4301:
        err.write(
            "[HINT] 4301 SSE_REPLAY_EXPIRED：断线重连超出回放窗口，不再续传——"
            "先经 GET /sessions/{id}/messages 拉全量历史校正本地状态，"
            "再不带 Last-Event-ID 重新订阅（api/02 §4）。\n"
        )
    return api_exit_code(exc)


# ================================================================ 工具


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """本地解码 JWT payload（base64url，不验签——CLI 仅显示身份，验签权威在网关）。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("JWT 段数不为 3")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    if not isinstance(claims, dict):
        raise ValueError("JWT payload 非对象")
    return claims


def _emit_json(ctx: Context, data: Any, meta: dict[str, Any]) -> None:
    """/--output json：顶层固定 {data, meta}（CLI设计 §6，可被 jq 消费，UTF-8）。"""
    ctx.stdout.write(json.dumps({"data": data, "meta": meta}, ensure_ascii=False, indent=2) + "\n")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _reconfigure_utf8(stream: TextIO) -> None:
    """stdout/stderr 强制 UTF-8（Windows 控制台 GBK 兜底；json 模式 UTF-8 为 CLI设计 §6 要求）。"""
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


# ================================================================ 命令实现


def cmd_healthz(args: argparse.Namespace, ctx: Context) -> int:
    """GET /healthz：存活探针（匿名，api/01 §5.0）。"""
    del args
    data, meta = ctx.client.request("GET", "/healthz", auth=False)
    if ctx.config.output == "json":
        _emit_json(ctx, data, meta)
        return EXIT_OK
    info = _as_dict(data)
    ctx.stdout.write(
        f"status={info.get('status', '?')} version={info.get('version', '?')} profile={info.get('profile', '?')}\n"
    )
    return EXIT_OK


def cmd_auth_login(args: argparse.Namespace, ctx: Context) -> int:
    """POST /auth/login：签发令牌对并写入配置文件（0600；api/01 §5.9）。"""
    email = str(args.email)
    password = os.environ.get(ENV_PASSWORD, "")
    if not password:
        try:
            password = getpass.getpass(f"{email} 的网关密码: ")
        except (EOFError, KeyboardInterrupt):
            ctx.stderr.write("[ERR] 未输入密码（CI 场景用 OA_CLI_PASSWORD 环境变量）\n")
            return EXIT_GENERIC
    data, _meta = ctx.client.request(
        "POST", "/auth/login", json_body={"email": email, "password": password}, auth=False
    )
    payload = _as_dict(data)
    access = str(payload.get("access_token", ""))
    if not access:
        ctx.stderr.write("[ERR] 登录响应缺少 access_token（核对网关版本）\n")
        return EXIT_GENERIC
    expires_in = payload.get("expires_in")
    path = save_tokens(
        access_token=access,
        refresh_token=str(payload["refresh_token"]) if payload.get("refresh_token") else None,
        email=email,
        expires_in=expires_in if isinstance(expires_in, int) else None,
    )
    ctx.stdout.write(f"登录成功：令牌已写入 {path}（权限 0600；Windows 平台权限尽力而为）\n")
    return EXIT_OK


def cmd_auth_whoami(args: argparse.Namespace, ctx: Context) -> int:
    """当前身份：本地解码 access token claims（/auth/me 端点未实现——api/01 §5.15 预登记中）。"""
    del args
    token = ctx.config.access_token
    if not token:
        ctx.stderr.write("[ERR] 未登录：先执行 onto auth login，或设 OA_CLI_TOKEN\n")
        return EXIT_AUTH
    try:
        claims = decode_jwt_claims(token)
    except (ValueError, UnicodeError):
        ctx.stderr.write("[ERR] 令牌不可解析：请重新登录\n")
        return EXIT_AUTH
    if ctx.config.output == "json":
        _emit_json(ctx, claims, {})
        return EXIT_OK
    for key in ("sub", "tenant_id", "roles", "scopes", "typ", "exp"):
        value = claims.get(key)
        if key == "exp" and isinstance(value, (int, float)):
            value = datetime.fromtimestamp(value, tz=UTC).isoformat()
        ctx.stdout.write(f"{key}={json.dumps(value, ensure_ascii=False)}\n")
    ctx.stdout.write("（本地解码未验签，仅供显示；权威校验在网关）\n")
    return EXIT_OK


def cmd_sessions_list(args: argparse.Namespace, ctx: Context) -> int:
    """GET /sessions：当前用户会话列表（api/01 §5.2；实现返回 {items, offset, limit}）。"""
    data, meta = ctx.client.request("GET", "/sessions", params={"offset": int(args.offset), "limit": int(args.limit)})
    payload = _as_dict(data)
    items = _as_list(payload.get("items"))
    if ctx.config.output == "json":
        _emit_json(ctx, items, {"count": len(items), **_as_dict(payload), **meta})
        return EXIT_OK
    ctx.stdout.write(f"{'ID':<38} {'STATUS':<10} {'TITLE':<24} AGENT\n")
    for item in items:
        row = _as_dict(item)
        title = truncate(str(row.get("title") or "-"), 24)
        ctx.stdout.write(
            f"{str(row.get('id', '?')):<38} {str(row.get('status', '?')):<10} {title:<24} {row.get('agent_id', '?')}\n"
        )
    ctx.stdout.write(
        f"共 {len(items)} 条（offset={payload.get('offset', args.offset)} limit={payload.get('limit', args.limit)}）\n"
    )
    return EXIT_OK


def cmd_sessions_create(args: argparse.Namespace, ctx: Context) -> int:
    """POST /sessions：创建会话（绑定 agent，api/01 §5.2）。"""
    body: dict[str, Any] = {"agent_id": str(args.agent), "channel": str(args.channel)}
    if args.title:
        body["title"] = str(args.title)
    data, meta = ctx.client.request("POST", "/sessions", json_body=body)
    payload = _as_dict(data)
    if ctx.config.output == "json":
        _emit_json(ctx, payload, meta)
        return EXIT_OK
    ctx.stdout.write(f"会话已创建 id={payload.get('id', '?')} status={payload.get('status', '?')}\n")
    return EXIT_OK


def cmd_sessions_send(args: argparse.Namespace, ctx: Context) -> int:
    """POST /sessions/{id}/messages：非流式=202 受理；--stream=SSE 主干波渲染（api/01 §6.1）。"""
    session_id = quote(str(args.session_id), safe="")
    body: dict[str, Any] = {"content": str(args.content), "adapter": str(args.adapter)}
    if args.stream:
        renderer = TrunkEventRenderer(ctx.stdout, ctx.stderr)
        for frame in ctx.client.stream_sse("POST", f"/sessions/{session_id}/messages", json_body=body):
            renderer.render(frame)
        renderer.finish()
        return EXIT_GENERIC if renderer.run_error is not None else EXIT_OK
    data, meta = ctx.client.request("POST", f"/sessions/{session_id}/messages", json_body=body)
    payload = _as_dict(data)
    if ctx.config.output == "json":
        _emit_json(ctx, payload, meta)
        return EXIT_OK
    ctx.stdout.write(
        f"已受理 run={payload.get('run_id', '?')} task={payload.get('task_id', '?')}"
        f" status={payload.get('status', '?')}\n"
    )
    ctx.stdout.write("（非流式受理：事件经 GET /sessions/{id}/events 订阅，或加 --stream 流式渲染）\n")
    return EXIT_OK


def cmd_kb_search(args: argparse.Namespace, ctx: Context) -> int:
    """POST /kb/search：GraphRAG 检索（api/01 §5.4；degraded 必须显式标注）。"""
    body: dict[str, Any] = {"query": str(args.query), "mode": str(args.mode), "top_k": int(args.top_k)}
    if args.kb_id:
        body["kb_id"] = str(args.kb_id)
    data, meta = ctx.client.request("POST", "/kb/search", json_body=body)
    payload = _as_dict(data)
    if ctx.config.output == "json":
        _emit_json(ctx, payload, meta)
        return EXIT_OK
    hits = _as_list(payload.get("hits"))
    citations = _as_list(payload.get("citations"))
    mode_used = payload.get("mode_used", payload.get("mode", "?"))
    ctx.stdout.write(
        f"检索完成 mode={mode_used} 命中 {len(hits)} 条（引用 {len(citations)}，{payload.get('latency_ms', '?')}ms）\n"
    )
    if payload.get("degraded"):
        reasons = ",".join(str(r) for r in _as_list(payload.get("degraded_reasons")))
        ctx.stdout.write(f"[WARN] 降级检索 degraded=true（{reasons or '原因未附'}）\n")
    for rank, hit in enumerate(hits[:5], start=1):
        row = _as_dict(hit)
        doc = row.get("doc_name") or row.get("document_id") or "?"
        content = str(row.get("content", "")).replace("\n", " ")
        ctx.stdout.write(
            f"  {rank}. score={row.get('score', '?')} doc={truncate(str(doc), 30)} {truncate(content, 60)}\n"
        )
    return EXIT_OK


def cmd_memory_context(args: argparse.Namespace, ctx: Context) -> int:
    """GET /memory/context：组装会话上下文记忆（api/01 §5.5/§6.6）。"""
    params: dict[str, Any] = {"session_id": str(args.session_id), "mode": str(args.mode)}
    if args.user_id:
        params["user_id"] = str(args.user_id)
    data, meta = ctx.client.request("GET", "/memory/context", params=params)
    payload = _as_dict(data)
    if ctx.config.output == "json":
        _emit_json(ctx, payload, meta)
        return EXIT_OK
    l1 = _as_dict(payload.get("l1"))
    window = _as_list(l1.get("window"))
    blocks = _as_list(l1.get("blocks"))
    parts = [f"L1 窗口 {len(window)} 条 / 块 {len(blocks)} 个"]
    for layer in ("l2", "l3", "l4"):
        parts.append(f"{layer.upper()} {len(_as_list(payload.get(layer)))} 条")
    mode = meta.get("mode", args.mode) if isinstance(meta, dict) else args.mode
    ctx.stdout.write(f"记忆上下文 mode={mode}：{'，'.join(parts)}\n")
    return EXIT_OK


def cmd_ontology_validate(args: argparse.Namespace, ctx: Context) -> int:
    """POST /ontologies/{id}/validate：SHACL 试校验；conforms=false → 退出码 2（CLI设计 §6）。

    请求体按当前实现 OntologyValidateIn（extra=forbid，仅收 data_graph Turtle 文本）；
    api/01 §6.3 的 data_graph_uri 字段为契约预登记，实现落地前不发。
    """
    graph_path = Path(str(args.data_graph))
    try:
        data_graph = graph_path.read_text(encoding="utf-8")
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        ctx.stderr.write(f"[ERR] 无法读取数据图文件 {graph_path}：{reason}\n")
        return EXIT_GENERIC
    ontology_id = quote(str(args.ontology_id), safe="")
    data, meta = ctx.client.request("POST", f"/ontologies/{ontology_id}/validate", json_body={"data_graph": data_graph})
    payload = _as_dict(data)
    conforms = bool(payload.get("conforms"))
    if ctx.config.output == "json":
        _emit_json(ctx, payload, meta)
        return EXIT_OK if conforms else EXIT_VALIDATION
    results = _as_list(payload.get("results"))
    if conforms:
        ctx.stdout.write(f"[OK] SHACL 校验通过（违例 0，{payload.get('elapsed_ms', '?')}ms）\n")
        return EXIT_OK
    ctx.stdout.write(f"[FAIL] SHACL 校验不通过：违例 {len(results)} 处\n")
    for item in results[:10]:
        row = _as_dict(item)
        ctx.stdout.write(
            f"  - [{row.get('severity', 'Violation')}] {row.get('constraint', '?')}"
            f" {truncate(str(row.get('message', '')), 80)}\n"
        )
    if len(results) > 10:
        ctx.stdout.write(f"  …（其余 {len(results) - 10} 处略，--output json 看全量）\n")
    return EXIT_VALIDATION


# ================================================================ 命令树装配


def _global_options(parser: argparse.ArgumentParser) -> None:
    """全局选项（SUPPRESS 缺省：根与子命令共享父解析器，未给时不覆盖）。"""
    parser.add_argument("--endpoint", help="网关基地址（默认 OA_CLI_ENDPOINT / 配置文件）", default=argparse.SUPPRESS)
    parser.add_argument(
        "--output", choices=("table", "json"), help="输出格式（默认 table；json 供 jq 消费）", default=argparse.SUPPRESS
    )
    parser.add_argument("--timeout", type=float, help="请求超时秒数（默认 30）", default=argparse.SUPPRESS)
    parser.add_argument(
        "--trace-id", help="链路 ID 透传（X-Request-ID，网关回显并贯穿审计）", default=argparse.SUPPRESS
    )
    parser.add_argument(
        "--config",
        help="配置文件路径覆盖（默认 OA_CLI_CONFIG / ~/.ontology-agent/config.json）",
        default=argparse.SUPPRESS,
    )


def build_parser() -> argparse.ArgumentParser:
    """命令树：healthz / auth(login·whoami) / sessions(list·create·send) / kb(search) /
    memory(context) / ontology(validate)。"""
    shared = argparse.ArgumentParser(add_help=False)
    _global_options(shared)
    parser = argparse.ArgumentParser(
        prog=PROG,
        parents=[shared],
        description="ontology-agent 平台 CLI（只调网关 REST API，不直连存储——架构锚点 §2）",
    )
    subs = parser.add_subparsers(dest="command", metavar="<命令>")

    healthz = subs.add_parser("healthz", parents=[shared], help="存活探针 GET /healthz（匿名）")
    healthz.set_defaults(func=cmd_healthz)

    auth = subs.add_parser("auth", parents=[shared], help="登录与身份（api/01 §5.9）")
    auth_subs = auth.add_subparsers(dest="subcommand", metavar="<子命令>", required=True)
    login = auth_subs.add_parser("login", parents=[shared], help="登录：签发令牌对并写入配置文件（0600）")
    login.add_argument("--email", required=True, help="登录邮箱")
    login.set_defaults(func=cmd_auth_login)
    whoami = auth_subs.add_parser("whoami", parents=[shared], help="当前身份（本地解码令牌 claims，不验签）")
    whoami.set_defaults(func=cmd_auth_whoami)

    sessions = subs.add_parser("sessions", parents=[shared], help="会话管理（api/01 §5.2）")
    ses_subs = sessions.add_subparsers(dest="subcommand", metavar="<子命令>", required=True)
    ses_list = ses_subs.add_parser("list", parents=[shared], help="当前用户会话列表")
    ses_list.add_argument("--offset", type=int, default=0, help="偏移（默认 0）")
    ses_list.add_argument("--limit", type=int, default=20, help="条数上限（默认 20，≤100）")
    ses_list.set_defaults(func=cmd_sessions_list)
    ses_create = ses_subs.add_parser("create", parents=[shared], help="创建会话（绑定 agent）")
    ses_create.add_argument("--agent", required=True, help="agent ID（UUID）")
    ses_create.add_argument("--title", help="会话标题")
    ses_create.add_argument("--channel", choices=("web", "api", "cli"), default="cli", help="渠道（默认 cli）")
    ses_create.set_defaults(func=cmd_sessions_create)
    ses_send = ses_subs.add_parser("send", parents=[shared], help="发送消息（--stream 走 SSE 主干波渲染）")
    ses_send.add_argument("session_id", help="会话 ID")
    ses_send.add_argument("content", help="消息内容")
    ses_send.add_argument("--stream", action="store_true", help="Accept: text/event-stream 流式渲染")
    ses_send.add_argument("--adapter", choices=("builtin", "claude"), default="builtin", help="适配器（默认 builtin）")
    ses_send.set_defaults(func=cmd_sessions_send)

    kb = subs.add_parser("kb", parents=[shared], help="知识库检索（api/01 §5.4）")
    kb_subs = kb.add_subparsers(dest="subcommand", metavar="<子命令>", required=True)
    kb_search = kb_subs.add_parser("search", parents=[shared], help="GraphRAG 检索 POST /kb/search")
    kb_search.add_argument("query", help="检索问题")
    kb_search.add_argument("--mode", choices=("auto", "local", "global", "drift"), default="auto", help="检索模式")
    kb_search.add_argument("--top-k", dest="top_k", type=int, default=8, help="召回条数（默认 8，≤50）")
    kb_search.add_argument("--kb-id", dest="kb_id", help="限定知识库集合 ID")
    kb_search.set_defaults(func=cmd_kb_search)

    memory = subs.add_parser("memory", parents=[shared], help="多层记忆（api/01 §5.5）")
    mem_subs = memory.add_subparsers(dest="subcommand", metavar="<子命令>", required=True)
    mem_ctx = mem_subs.add_parser("context", parents=[shared], help="组装会话上下文记忆 GET /memory/context")
    mem_ctx.add_argument("--session", dest="session_id", required=True, help="会话 ID")
    mem_ctx.add_argument("--mode", choices=("full", "light"), default="full", help="组装模式（默认 full）")
    mem_ctx.add_argument("--user", dest="user_id", help="用户 ID（缺省取令牌主体）")
    mem_ctx.set_defaults(func=cmd_memory_context)

    ontology = subs.add_parser("ontology", parents=[shared], help="本体域（api/01 §5.3）")
    ont_subs = ontology.add_subparsers(dest="subcommand", metavar="<子命令>", required=True)
    ont_validate = ont_subs.add_parser("validate", parents=[shared], help="SHACL 试校验（不符退出码 2）")
    ont_validate.add_argument("ontology_id", help="本体 ID（UUID）")
    ont_validate.add_argument("--data-graph", dest="data_graph", required=True, help="Turtle 数据图文件路径")
    ont_validate.set_defaults(func=cmd_ontology_validate)

    return parser


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """进程入口：解析→装配→分发；错误统一透出 stderr 并按 §6 映射退出码。"""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    _reconfigure_utf8(out)
    _reconfigure_utf8(err)
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(out)
        return EXIT_GENERIC
    config = load_config(getattr(args, "config", None))
    endpoint = getattr(args, "endpoint", None)
    if endpoint:
        config.endpoint = str(endpoint).rstrip("/")
    output = getattr(args, "output", None)
    if output:
        config.output = str(output)
    timeout = getattr(args, "timeout", None)
    if timeout:
        config.timeout_seconds = float(timeout)
    ctx = Context(
        config=config,
        client=ApiClient(config, trace_id=getattr(args, "trace_id", None), transport=transport),
        stdout=out,
        stderr=err,
    )
    try:
        return int(func(args, ctx))
    except ApiError as exc:
        return report_api_error(exc, err)
    except (httpx.TimeoutException, httpx.TransportError) as exc:  # 双保险：客户端兜底之外的传输异常
        err.write(f"[ERR] 网络错误：{type(exc).__name__}\n")
        return EXIT_NETWORK
