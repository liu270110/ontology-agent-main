"""oa CLI 骨架测试（CliRunner + httpx.MockTransport，零外网）。

覆盖（standards/01 §2.9：AAA 布局、中文命名）：
- login：成功写凭据文件（含 0600 语义断言，仅 POSIX）；凭据无效退出码 3 且不落凭据；
- kb search：携带 Bearer 凭据与请求体契约（query/top_k/kb_id），citations 表格化渲染，
  citations 缺省时回落 hits 投影；未登录调用受保护命令 → 明确报错 + 退出码 2；
- whoami：正常渲染身份；token 被拒（401）→ 退出码 3；
- status：healthz 渲染；服务不可达（ConnectError）→ 退出码 3；
- ontology list：items 表格化渲染。
退出码语义权威=services/cli/oa.py 模块 docstring（13 篇 §4：0/1/2/3/4）。
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner, Result

from services.cli import oa

BASE_URL = "http://gateway.test"
USERNAME = "ops@example.com"
_ACCESS_TOKEN = "test-access-token"

AuthHandler = Callable[[httpx.Request], httpx.Response]


# --------------------------------------------------------------- 夹具与小工具


@pytest.fixture()
def creds_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把凭据文件指到临时目录，并隔离 OA_BASE_URL 环境变量（保证基址确定性）。"""
    path = tmp_path / ".oa" / "credentials.json"
    monkeypatch.setattr(oa, "CREDENTIALS_PATH", path)
    monkeypatch.delenv("OA_BASE_URL", raising=False)
    return path


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: AuthHandler) -> None:
    """monkeypatch httpx 传输层：CLI 构建的 client 一律走 MockTransport，零外网。"""

    def fake_client(base_url: str) -> httpx.Client:
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(oa, "_make_client", fake_client)


def _seed_credentials(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "username": USERNAME,
                "base_url": BASE_URL,
                "access_token": _ACCESS_TOKEN,
                "refresh_token": "test-refresh-token",
                "expires_in": 7200,
            }
        ),
        encoding="utf-8",
    )


def _all_output(result: Result) -> str:
    """stdout + stderr 合并（兼容 click 8.1 混合流与 8.2+ 分离流两种 Result 行为）。"""
    out = result.output
    try:
        out += result.stderr
    except ValueError:
        pass
    return out


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(oa.cli, list(args))


def _search_payload() -> dict:
    return {
        "query": "停电影响范围",
        "mode": "auto",
        "mode_used": "local",
        "degraded": False,
        "degraded_reasons": [],
        "channels": ["bm25"],
        "latency_ms": 12,
        "hits": [],
        "citations": [
            {
                "chunk_id": "11111111-1111-1111-1111-111111111111",
                "doc_id": "22222222-2222-2222-2222-222222222222",
                "doc_name": "停电预案.pdf",
                "minio_key": None,
                "quote": "10kV 馈线 F12 故障跳闸，影响城东 3 个台区",
                "span": [0, 24],
                "score": 0.91,
            },
            {
                "chunk_id": "33333333-3333-3333-3333-333333333333",
                "doc_id": "22222222-2222-2222-2222-222222222222",
                "doc_name": "抢修工单.md",
                "minio_key": None,
                "quote": "F12 抢修预计 2 小时恢复送电",
                "span": [0, 16],
                "score": 0.42,
            },
        ],
        "evidence": {"graph_paths": [], "community_reports": []},
        "answers": [],
        "usage": {"latency_ms": 12, "llm_calls": 0},
    }


# --------------------------------------------------------------- login


def test_login_success_writes_credentials_file(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={"access_token": "acc-1", "refresh_token": "ref-1", "token_type": "bearer", "expires_in": 7200},
        )

    _patch_transport(monkeypatch, handler)
    result = _invoke("login", "--username", USERNAME, "--password", "secret", "--base-url", BASE_URL)

    assert result.exit_code == 0, _all_output(result)  # 0=成功
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/auth/login"
    assert captured["body"] == {"email": USERNAME, "password": "secret"}  # 服务端契约字段=email
    assert creds_path.is_file(), "登录成功必须落凭据文件"
    saved = json.loads(creds_path.read_text(encoding="utf-8"))
    assert saved["access_token"] == "acc-1"
    assert saved["refresh_token"] == "ref-1"
    assert saved["base_url"] == BASE_URL
    assert "登录成功" in _all_output(result)
    if os.name == "posix":  # 0600 语义（Windows 文件权限模型不同，尽力而为）
        assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600


def test_login_invalid_credentials_exits_3(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 1002, "message": "凭据无效"})

    _patch_transport(monkeypatch, handler)
    result = _invoke("login", "--username", USERNAME, "--password", "wrong", "--base-url", BASE_URL)

    assert result.exit_code == 3, _all_output(result)  # 3=认证/网络错误
    assert not creds_path.exists(), "登录失败不得落凭据文件"
    assert "凭据" in _all_output(result)


def test_login_unreachable_exits_3(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_transport(monkeypatch, handler)
    result = _invoke("login", "--username", USERNAME, "--password", "secret", "--base-url", BASE_URL)

    assert result.exit_code == 3, _all_output(result)
    assert "网络错误" in _all_output(result)


# --------------------------------------------------------------- 未登录前置（用法错误=2）


def test_protected_command_without_login_exits_2_with_clear_message(creds_path: Path) -> None:
    for args in (["whoami"], ["kb", "search", "任意查询"], ["ontology", "list"]):
        result = _invoke(*args)
        assert result.exit_code == 2, f"{args}: {_all_output(result)}"  # 2=用法错误（本地前置不满足）
        assert "未登录" in _all_output(result) and "oa login" in _all_output(result)


# --------------------------------------------------------------- kb search


def test_kb_search_renders_citations_table(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_credentials(creds_path)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json=_search_payload())

    _patch_transport(monkeypatch, handler)
    result = _invoke("kb", "search", "停电影响范围", "--kb-id", "44444444-4444-4444-4444-444444444444", "--top-k", "2")

    assert result.exit_code == 0, _all_output(result)
    assert captured["path"] == "/api/v1/kb/search"
    assert captured["auth"] == f"Bearer {_ACCESS_TOKEN}"
    assert captured["body"] == {
        "query": "停电影响范围",
        "top_k": 2,
        "kb_id": "44444444-4444-4444-4444-444444444444",
    }
    out = _all_output(result)
    assert "引用 2 条" in out
    assert "停电预案.pdf" in out and "抢修工单.md" in out  # 引用表格化：doc 列
    assert "0.9100" in out and "0.4200" in out  # score 列（四位小数）
    assert "馈线 F12" in out  # quote 列


def test_kb_search_falls_back_to_hits_projection(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_credentials(creds_path)
    payload = _search_payload()
    payload["citations"] = []  # 旧形态：仅 hits 投影
    payload["hits"] = [
        {
            "chunk_id": "55555555-5555-5555-5555-555555555555",
            "document_id": "66666666-6666-6666-6666-666666666666",
            "content": "故障录波显示 B 相接地",
            "score": 0.66,
            "doc_name": "录波报告.txt",
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    _patch_transport(monkeypatch, handler)
    result = _invoke("kb", "search", "接地故障")

    assert result.exit_code == 0, _all_output(result)
    assert "引用 1 条" in _all_output(result)
    assert "录波报告.txt" in _all_output(result)


# --------------------------------------------------------------- whoami / status / ontology list


def test_whoami_renders_identity(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_credentials(creds_path)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "user_id": "77777777-7777-7777-7777-777777777777",
                "email": USERNAME,
                "tenant_id": "88888888-8888-8888-8888-888888888888",
                "roles": ["ops"],
                "scopes": ["kb:read", "ontology:read"],
            },
        )

    _patch_transport(monkeypatch, handler)
    result = _invoke("whoami")

    assert result.exit_code == 0, _all_output(result)
    assert captured["path"] == "/api/v1/auth/me"
    assert captured["auth"] == f"Bearer {_ACCESS_TOKEN}"
    out = _all_output(result)
    assert USERNAME in out and "roles: ops" in out and "kb:read" in out


def test_whoami_rejected_token_exits_3(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_credentials(creds_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 2002, "message": "access token 过期"})

    _patch_transport(monkeypatch, handler)
    result = _invoke("whoami")

    assert result.exit_code == 3, _all_output(result)  # 3=认证失败
    assert "oa login" in _all_output(result)


def test_status_reports_gateway_health(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "0.2.0-m1", "profile": "lite"})

    _patch_transport(monkeypatch, handler)
    result = _invoke("status", "--base-url", BASE_URL)

    assert result.exit_code == 0, _all_output(result)
    out = _all_output(result)
    assert "ok" in out and "0.2.0-m1" in out and "lite" in out


def test_status_unreachable_exits_3(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_transport(monkeypatch, handler)
    result = _invoke("status", "--base-url", BASE_URL)

    assert result.exit_code == 3, _all_output(result)
    assert "网络错误" in _all_output(result)


def test_ontology_list_renders_table(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_credentials(creds_path)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "99999999-9999-9999-9999-999999999999",
                        "iri_base": "https://ont.oa.dev/power/",
                        "name": "电力域本体",
                        "description": None,
                        "scheme_tier": "domain",
                        "status": "published",
                        "head_version": {"version": "v3", "changeset_id": None},
                        "active_changeset": None,
                    },
                    {
                        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "iri_base": "https://ont.oa.dev/grid/",
                        "name": "配网拓扑本体",
                        "description": None,
                        "scheme_tier": "domain",
                        "status": "draft",
                        "head_version": None,
                        "active_changeset": None,
                    },
                ],
                "offset": 0,
                "limit": 20,
            },
        )

    _patch_transport(monkeypatch, handler)
    result = _invoke("ontology", "list", "--limit", "20")

    assert result.exit_code == 0, _all_output(result)
    assert captured["path"] == "/api/v1/ontologies"
    assert captured["auth"] == f"Bearer {_ACCESS_TOKEN}"
    assert captured["params"] == {"limit": "20"}
    out = _all_output(result)
    assert "共 2 个本体" in out
    assert "电力域本体" in out and "配网拓扑本体" in out
    assert "published" in out and "draft" in out
    assert "v3" in out  # head_version.version 列
