"""oa —— ontology-agent 平台 CLI（REST 薄壳，Click 骨架）。

命令树与退出码对齐 docs/架构设计/13-接口契约REST端点与CLI命令全集.md §4（`oa` CLI 命令树全集）；
REST 端点对齐 services/gateway 实装路由：auth（services/iam/api/auth.py）、kb（services/kb/api/kb.py，
响应 schema=services/kb/api/schemas/kb.py KbSearchOut）、ontology（services/ontology/api/ontology.py）、
探活（services/gateway/app.py /api/v1/healthz）。whoami 对应 GET /api/v1/auth/me（登记册已登记，
服务端随批次补齐，CLI 先行对齐契约）。

退出码语义（13 §4，源自 05 篇 §4.2，全命令统一）：
    0 成功；
    1 业务错误（服务端 4xx/5xx 错误体，透传 3xxx/4xxx 等业务段）；
    2 用法错误（参数缺失/越界——Click 自身亦用 2；含未登录调用受保护命令，即本地前置条件不满足）；
    3 认证/网络错误（登录凭据无效、401/403、连接不可达、超时）；
    4 部分失败（批量场景保留，当前命令集不触发）。

凭据：~/.oa/credentials.json（0600 语义：POSIX chmod 0600，Windows 下尽力而为）。
基址：--base-url 或环境变量 OA_BASE_URL 覆盖，默认 http://localhost:8000；API 前缀 /api/v1（02 §4）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, NoReturn

import click
import httpx

DEFAULT_BASE_URL = "http://localhost:8000"
_API_PREFIX = "/api/v1"
_TIMEOUT_S = 10.0
_QUOTE_WIDTH = 48
_IRI_WIDTH = 40

# 退出码常量（语义见模块 docstring，勿在命令内散落魔法数）
EXIT_OK = 0
EXIT_BUSINESS = 1
EXIT_USAGE = 2
EXIT_AUTH_NETWORK = 3
EXIT_PARTIAL = 4  # 保留语义占位（批量部分失败），当前命令集不触发；仅 docstring/日志引用

CREDENTIALS_PATH = Path.home() / ".oa" / "credentials.json"


# --------------------------------------------------------------- 退出与凭据小工具


def _fail(message: str, exit_code: int) -> NoReturn:
    """统一错误出口：stderr 输出 + 指定退出码（语义见模块 docstring）。"""
    click.echo(f"错误: {message}", err=True)
    ctx = click.get_current_context()
    ctx.exit(exit_code)


def _load_credentials() -> dict[str, Any]:
    """读本地凭据；未登录/文件损坏按「用法错误」（本地前置条件不满足）退出 2。"""
    path = CREDENTIALS_PATH
    if not path.is_file():
        _fail(f"未登录：未找到凭据文件 {path}，请先执行 oa login", EXIT_USAGE)
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"凭据文件损坏或不可读：{path}（{exc}），请重新执行 oa login", EXIT_USAGE)
    if not isinstance(creds, dict) or not creds.get("access_token"):
        _fail(f"凭据文件缺少 access_token：{path}，请重新执行 oa login", EXIT_USAGE)
    return creds


def _save_credentials(creds: dict[str, Any]) -> Path:
    """写凭据文件（0600 语义：os.open mode + POSIX chmod 兜底；Windows 尽力而为）。"""
    path = CREDENTIALS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)
        f.write("\n")
    if os.name == "posix":
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path


# --------------------------------------------------------------- HTTP 薄封装


def _make_client(base_url: str) -> httpx.Client:
    """构建网关 HTTP 客户端（测试经 monkeypatch 本函数注入 MockTransport，零外网）。"""
    return httpx.Client(base_url=base_url, timeout=_TIMEOUT_S)


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return client.request(method, path, json=json_body, params=params, headers=headers)


def _server_message(resp: httpx.Response) -> str:
    """提取网关统一错误体的 message/code（02 §6），便于透传业务段错误。"""
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(data, dict) and data.get("message"):
        code = data.get("code")
        return f"code={code} {data['message']}" if code is not None else str(data["message"])
    return resp.text[:200]


def _json_or_fail(resp: httpx.Response, what: str) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        _fail(f"{what}：服务端响应非 JSON（HTTP {resp.status_code}）", EXIT_BUSINESS)
    if not isinstance(data, dict):
        _fail(f"{what}：服务端响应结构异常（HTTP {resp.status_code}）", EXIT_BUSINESS)
    return data


def _authed_json_or_fail(resp: httpx.Response, what: str, creds: dict[str, Any]) -> dict[str, Any]:
    """受保护命令响应分流：401/403→3（认证失败），其余 4xx/5xx→1（业务错误）。"""
    if resp.status_code in (401, 403):
        _fail(f"{what}：凭据无效或已过期（HTTP {resp.status_code}），请重新执行 oa login", EXIT_AUTH_NETWORK)
    if resp.status_code >= 400:
        _fail(f"{what}：服务端业务错误（HTTP {resp.status_code}）：{_server_message(resp)}", EXIT_BUSINESS)
    return _json_or_fail(resp, what)


# --------------------------------------------------------------- 渲染小工具


def _truncate(text: str, width: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: max(width - 3, 1)] + "..."


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    """纯文本定宽表格（默认人类可读输出，13 §4：--output json/yaml 随批次补齐）。"""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), "  ".join("-" * w for w in widths)]
    lines.extend(fmt.format(*row) for row in rows)
    return "\n".join(lines)


# --------------------------------------------------------------- 命令树（13 §4）


@click.group(name="oa")
def cli() -> None:
    """oa —— ontology-agent 平台 CLI（只调网关 REST，不在本机直连五存储）。"""


@cli.command()
@click.option("--username", "-u", prompt=True, help="登录用户名（服务端登录契约为 email 字段）")
@click.option("--password", "-p", prompt=True, hide_input=True, help="登录密码（隐藏输入）")
@click.option("--base-url", envvar="OA_BASE_URL", default=DEFAULT_BASE_URL, show_default=True, help="网关基址")
def login(username: str, password: str, base_url: str) -> None:
    """用户名密码换 JWT 并写入本地凭据（POST /api/v1/auth/login）。"""
    try:
        with _make_client(base_url) as client:
            resp = client.request("POST", f"{_API_PREFIX}/auth/login", json={"email": username, "password": password})
    except httpx.HTTPError as exc:
        _fail(f"网络错误：无法连接 {base_url}（{exc}）", EXIT_AUTH_NETWORK)
    if resp.status_code in (401, 403):
        _fail(f"登录失败：凭据无效（HTTP {resp.status_code}）", EXIT_AUTH_NETWORK)
    if resp.status_code >= 400:
        _fail(f"登录失败：服务端错误（HTTP {resp.status_code}）：{_server_message(resp)}", EXIT_BUSINESS)
    data = _json_or_fail(resp, "login")
    path = _save_credentials(
        {
            "username": username,
            "base_url": base_url,
            "access_token": data.get("access_token", ""),
            "refresh_token": data.get("refresh_token", ""),
            "token_type": data.get("token_type", "bearer"),
            "expires_in": data.get("expires_in"),
        }
    )
    expires = data.get("expires_in")
    suffix = f"，access 有效期 {expires}s" if isinstance(expires, int) else ""
    click.echo(f"登录成功：{username} @ {base_url}{suffix}；凭据已写入 {path}")


@cli.command()
@click.option("--base-url", envvar="OA_BASE_URL", default=DEFAULT_BASE_URL, show_default=True, help="网关基址")
def whoami(base_url: str) -> None:
    """查看当前登录身份（GET /api/v1/auth/me）。"""
    creds = _load_credentials()
    try:
        with _make_client(base_url) as client:
            resp = _request(client, "GET", f"{_API_PREFIX}/auth/me", token=str(creds["access_token"]))
    except httpx.HTTPError as exc:
        _fail(f"网络错误：无法连接 {base_url}（{exc}）", EXIT_AUTH_NETWORK)
    data = _authed_json_or_fail(resp, "whoami", creds)
    shown = False
    for key in ("user_id", "sub", "email", "username", "tenant_id"):
        if data.get(key) is not None:
            click.echo(f"{key}: {data[key]}")
            shown = True
    for key in ("roles", "scopes"):
        value = data.get(key)
        if isinstance(value, list) and value:
            click.echo(f"{key}: {', '.join(str(v) for v in value)}")
            shown = True
    if not shown:  # 服务端 /me 契约字段未定稿前的兜底：原样结构化输出
        click.echo(json.dumps(data, ensure_ascii=False, indent=2))


@cli.command()
@click.option("--base-url", envvar="OA_BASE_URL", default=DEFAULT_BASE_URL, show_default=True, help="网关基址")
def status(base_url: str) -> None:
    """探测网关存活与版本（GET /api/v1/healthz，匿名）。"""
    try:
        with _make_client(base_url) as client:
            resp = _request(client, "GET", f"{_API_PREFIX}/healthz")
    except httpx.HTTPError as exc:
        _fail(f"网络错误：无法连接 {base_url}（{exc}）", EXIT_AUTH_NETWORK)
    if resp.status_code >= 400:
        _fail(f"status：服务端错误（HTTP {resp.status_code}）：{_server_message(resp)}", EXIT_BUSINESS)
    data = _json_or_fail(resp, "status")
    click.echo(
        f"gateway: {data.get('status', '?')}  version: {data.get('version', '?')}  profile: {data.get('profile', '?')}"
    )


@cli.group()
def kb() -> None:
    """知识库（kb）命令组。"""


@kb.command("search")
@click.argument("query")
@click.option("--kb-id", default=None, help="知识库集合 ID（缺省=租户内全库）")
@click.option("--top-k", default=8, show_default=True, type=click.IntRange(1, 50), help="返回条数上限")
@click.option("--base-url", envvar="OA_BASE_URL", default=DEFAULT_BASE_URL, show_default=True, help="网关基址")
def kb_search(query: str, kb_id: str | None, top_k: int, base_url: str) -> None:
    """混合检索并表格化输出 citations（POST /api/v1/kb/search，schema=KbSearchOut）。"""
    creds = _load_credentials()
    body: dict[str, Any] = {"query": query, "top_k": top_k}
    if kb_id:
        body["kb_id"] = kb_id
    try:
        with _make_client(base_url) as client:
            resp = _request(
                client, "POST", f"{_API_PREFIX}/kb/search", token=str(creds["access_token"]), json_body=body
            )
    except httpx.HTTPError as exc:
        _fail(f"网络错误：无法连接 {base_url}（{exc}）", EXIT_AUTH_NETWORK)
    data = _authed_json_or_fail(resp, "kb search", creds)

    citations = data.get("citations") or []
    rows: list[list[str]] = []
    for i, c in enumerate(citations, start=1):
        rows.append(
            [
                str(i),
                f"{float(c.get('score', 0)):.4f}",
                _truncate(c.get("doc_name") or "-", 32),
                str(c.get("chunk_id", "-"))[:8],
                _truncate(c.get("quote", ""), _QUOTE_WIDTH),
            ]
        )
    if not rows:  # 兼容保留投影：hits（content↔quote、document_id↔doc_id，见 kb.py schema docstring）
        for i, h in enumerate(data.get("hits") or [], start=1):
            rows.append(
                [
                    str(i),
                    f"{float(h.get('score', 0)):.4f}",
                    _truncate(h.get("doc_name") or "-", 32),
                    str(h.get("chunk_id", "-"))[:8],
                    _truncate(h.get("content", ""), _QUOTE_WIDTH),
                ]
            )
    usage = data.get("usage") or {}
    click.echo(
        f"query: {data.get('query', query)}  mode: {data.get('mode_used', '?')}"
        f"  latency: {usage.get('latency_ms', '?')}ms  引用 {len(rows)} 条"
    )
    if data.get("degraded"):
        reasons = ", ".join(data.get("degraded_reasons") or [])
        click.echo(f"警告: 检索已降级（{reasons}）", err=True)
    if not rows:
        click.echo("无结果")
        return
    click.echo(_render_table(["#", "score", "doc", "chunk", "quote"], rows))


@cli.group()
def ontology() -> None:
    """本体（ontology）命令组。"""


@ontology.command("list")
@click.option("--status", "status_filter", default=None, help="按状态过滤（如 draft/published）")
@click.option("--limit", default=20, show_default=True, type=click.IntRange(1, 100), help="分页大小")
@click.option("--base-url", envvar="OA_BASE_URL", default=DEFAULT_BASE_URL, show_default=True, help="网关基址")
def ontology_list(status_filter: str | None, limit: int, base_url: str) -> None:
    """列出租户内本体（GET /api/v1/ontologies，含当前发布版本）。"""
    creds = _load_credentials()
    params: dict[str, Any] = {"limit": limit}
    if status_filter:
        params["status"] = status_filter
    try:
        with _make_client(base_url) as client:
            resp = _request(client, "GET", f"{_API_PREFIX}/ontologies", token=str(creds["access_token"]), params=params)
    except httpx.HTTPError as exc:
        _fail(f"网络错误：无法连接 {base_url}（{exc}）", EXIT_AUTH_NETWORK)
    data = _authed_json_or_fail(resp, "ontology list", creds)
    items = data.get("items") or []
    rows = [
        [
            str(i.get("id", "-"))[:8],
            _truncate(i.get("name", "-"), 24),
            str(i.get("status", "-")),
            str((i.get("head_version") or {}).get("version") or "-"),
            _truncate(i.get("iri_base", "-"), _IRI_WIDTH),
        ]
        for i in items
    ]
    click.echo(f"共 {len(rows)} 个本体（offset={data.get('offset', 0)}, limit={data.get('limit', limit)}）")
    if not rows:
        click.echo("无结果")
        return
    click.echo(_render_table(["id", "name", "status", "version", "iri_base"], rows))


if __name__ == "__main__":
    cli()
