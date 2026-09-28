"""cli.app 命令端到端单测（argparse 命令树 + MockTransport 假网关；AAA、中文命名、禁真连）。

覆盖：登录端到端（令牌落盘且不回显）、whoami 本地解码、healthz 路径前缀、sessions 列表/
受理/SSE 流渲染、kb 检索、memory 上下文、ontology validate 退出码 2、4301 重连指引、
退出码映射、--output json 信封、配置令牌注入 Bearer 头。
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.cli.app import (
    EXIT_AUTH,
    EXIT_FORBIDDEN,
    EXIT_GENERIC,
    EXIT_NETWORK,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_VALIDATION,
    api_exit_code,
    decode_jwt_claims,
    main,
)
from services.cli.client import ApiError

ENDPOINT = "http://gw.test/api/v1"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """隔离 OA_CLI_*：防开发机真实环境泄漏进用例。"""
    for key in (
        "OA_CLI_ENDPOINT",
        "OA_CLI_TOKEN",
        "OA_CLI_OUTPUT",
        "OA_CLI_TIMEOUT",
        "OA_CLI_CONFIG",
        "OA_CLI_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _run(
    argv: list[str],
    handler: Any,
    monkeypatch: pytest.MonkeyPatch,
    config_file: Path | None = None,
) -> tuple[int, str, str]:
    """跑一条命令：注入 MockTransport 与输出流；config_file 隔离配置文件。"""
    if config_file is not None:
        monkeypatch.setenv("OA_CLI_CONFIG", str(config_file))
    else:
        monkeypatch.setenv("OA_CLI_CONFIG", str(Path("-unused-") / "cli-it.json"))
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdout=out, stderr=err, transport=httpx.MockTransport(handler))
    return code, out.getvalue(), err.getvalue()


def _b64url(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")


# ================================================================ auth


def test_auth_login_端到端_令牌落盘且输出不回显令牌(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange：CI 密码走环境变量；假网关返回令牌对（api/01 §5.9）
    clean_env.setenv("OA_CLI_PASSWORD", "pw-123")
    cfg_file = tmp_path / "config.json"
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"access_token": "acc-xyz", "refresh_token": "ref-xyz", "token_type": "bearer", "expires_in": 7200},
        )

    # Act
    code, out, err = _run(["auth", "login", "--email", "dev@x.cn"], handler, clean_env, cfg_file)
    # Assert：成功、令牌落盘、任何输出流不含令牌明文（安全验收）
    assert code == EXIT_OK
    assert seen["path"] == "/api/v1/auth/login"
    assert seen["body"] == {"email": "dev@x.cn", "password": "pw-123"}
    assert "登录成功" in out
    assert "acc-xyz" not in out and "acc-xyz" not in err
    assert "ref-xyz" not in out and "ref-xyz" not in err
    stored = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert stored["auth"]["access_token"] == "acc-xyz"
    assert stored["auth"]["refresh_token"] == "ref-xyz"


def test_auth_whoami_本地解码claims不验签(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange：配置文件预置手造 JWT（sub/tenant_id/roles）
    cfg_file = tmp_path / "config.json"
    claims = {"sub": "user-1234", "tenant_id": "t-1", "roles": ["curator"], "exp": 1795900000}
    jwt = f"{_b64url({'alg': 'HS256'})}.{_b64url(claims)}.sig"
    cfg_file.write_text(json.dumps({"auth": {"access_token": jwt}}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应发起任何请求
        raise AssertionError("whoami 不应发起网络请求")

    # Act
    code, out, _err = _run(["auth", "whoami"], handler, clean_env, cfg_file)
    # Assert
    assert code == EXIT_OK
    assert "user-1234" in out
    assert "curator" in out
    assert "未验签" in out


def test_auth_whoami_未登录时退出码4并提示登录(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange：空配置（无令牌、无 OA_CLI_TOKEN）
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("未登录不应发起网络请求")

    # Act
    code, _out, err = _run(["auth", "whoami"], handler, clean_env, cfg_file)
    # Assert
    assert code == EXIT_AUTH
    assert "auth login" in err


def test_decode_jwt_claims_非三段令牌抛ValueError():
    # Act / Assert
    with pytest.raises(ValueError):
        decode_jwt_claims("not-a-jwt")


# ================================================================ healthz


def test_healthz_命中网关v1前缀并输出状态(clean_env: pytest.MonkeyPatch):
    # Arrange
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"status": "ok", "version": "0.1.0", "profile": "lite"})

    # Act
    code, out, _err = _run(["healthz"], handler, clean_env)
    # Assert：路径=/api/v1/healthz（只经网关），匿名不带 Bearer 已在 client 用例覆盖
    assert code == EXIT_OK
    assert seen["path"] == "/api/v1/healthz"
    assert "status=ok" in out
    assert "lite" in out


def test_json输出_顶层data_meta结构可被jq消费(clean_env: pytest.MonkeyPatch):
    # Arrange
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    # Act：根级全局选项 --output json（CLI设计 §6：顶层固定 {data, meta}）
    code, out, _err = _run(["--output", "json", "healthz"], handler, clean_env)
    # Assert
    assert code == EXIT_OK
    parsed = json.loads(out)
    assert set(parsed) == {"data", "meta"}
    assert parsed["data"]["status"] == "ok"


# ================================================================ sessions


def test_sessions_list_渲染条目与统计行(clean_env: pytest.MonkeyPatch):
    # Arrange：实现返回 {items, offset, limit}（未包裹信封）
    items = [
        {"id": "s-aaa", "status": "active", "title": "停电分析", "agent_id": "a-1"},
        {"id": "s-bbb", "status": "closed", "title": None, "agent_id": "a-2"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["offset"] == "0"
        assert request.url.params["limit"] == "20"
        return httpx.Response(200, json={"items": items, "offset": 0, "limit": 20})

    # Act
    code, out, _err = _run(["sessions", "list"], handler, clean_env)
    # Assert
    assert code == EXIT_OK
    assert "s-aaa" in out and "s-bbb" in out
    assert "共 2 条" in out


def test_sessions_create_提交agent与渠道(clean_env: pytest.MonkeyPatch):
    # Arrange
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "s-new", "agent_id": "a-9", "status": "created", "title": None})

    # Act
    code, out, _err = _run(["sessions", "create", "--agent", "a-9", "--title", "排障"], handler, clean_env)
    # Assert
    assert code == EXIT_OK
    assert seen["body"] == {"agent_id": "a-9", "channel": "cli", "title": "排障"}
    assert "s-new" in out


def test_sessions_send_非流式_202受理解析run_id(clean_env: pytest.MonkeyPatch):
    # Arrange
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("accept")
        seen["path"] = request.url.path
        return httpx.Response(202, json={"data": {"run_id": "r9", "task_id": "t9", "status": "queued"}, "meta": {}})

    # Act
    code, out, _err = _run(["sessions", "send", "s-1", "分析停电原因"], handler, clean_env)
    # Assert：非流式路径不带 SSE Accept；信封解包出 run_id
    assert code == EXIT_OK
    assert seen["path"] == "/api/v1/sessions/s-1/messages"
    assert seen["accept"] != "text/event-stream"
    assert "r9" in out
    assert "queued" in out


def test_sessions_send_stream_渲染文本增量与usage(clean_env: pytest.MonkeyPatch):
    # Arrange：主干波 SSE 流（分两片到达，验证流式解析 + 渲染）
    sse = (
        b'id: 1\nevent: RUN_STARTED\ndata: {"run_id":"r1","task_id":"t1"}\n\n'
        b'id: 2\nevent: TEXT_MESSAGE_CONTENT\ndata: {"message_id":"m1","delta":"\\u4f60\\u597d"}\n\n'
        b'id: 3\nevent: TEXT_MESSAGE_CONTENT\ndata: {"message_id":"m1","delta":"\\uff0c\\u672c\\u4f53"}\n\n'
        b'id: 4\nevent: TEXT_MESSAGE_END\ndata: {"message_id":"m1","finish_reason":"stop"}\n\n'
        b'id: 5\nevent: RETRIEVAL_EVIDENCE\ndata: {"chunks":[{}],"graph_paths":[],"citations":[{}],'
        b'"degraded":false}\n\n'
        b'id: 6\nevent: RUN_FINISHED\ndata: {"run_id":"r1","usage":{"tokens":42,"cost":0.01}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "text/event-stream"
        assert json.loads(request.content)["content"] == "分析停电原因"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=iter([sse[:25], sse[25:]]))

    # Act
    code, out, _err = _run(["sessions", "send", "s-1", "分析停电原因", "--stream"], handler, clean_env)
    # Assert：增量拼成整句、证据折叠计数、usage 展示
    assert code == EXIT_OK
    assert "你好，本体" in out
    assert "切片 1" in out
    assert "42" in out


def test_sessions_send_stream_4301回放过期_提示重连且退出码3(clean_env: pytest.MonkeyPatch):
    # Arrange：网关对超窗重连返回 4301（HTTP 410，api/01 §4.3）
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"code": 4301, "message": "SSE 回放窗口过期", "trace_id": "req_9"})

    # Act
    code, _out, err = _run(["sessions", "send", "s-1", "hi", "--stream"], handler, clean_env)
    # Assert：网络族退出码 + 重连指引（不再续传、拉历史、不带 Last-Event-ID 重订阅）
    assert code == EXIT_NETWORK
    assert "4301" in err
    assert "重新订阅" in err
    assert "req_9" in err


# ================================================================ kb / memory / ontology


def test_kb_search_渲染命中数_实际模式与降级标注(clean_env: pytest.MonkeyPatch):
    # Arrange：KbSearchOut 形态（mode_used/degraded/degraded_reasons/hits/citations）
    body = {
        "query": "停电处置",
        "mode": "drift",
        "mode_used": "local",
        "degraded": True,
        "degraded_reasons": ["vector_unavailable"],
        "channels": ["bm25"],
        "latency_ms": 12,
        "hits": [{"chunk_id": "c1", "document_id": "d1", "content": "单相接地故障处置流程", "score": 0.9}],
        "citations": [{"doc_id": "d1"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert sent == {"query": "停电处置", "mode": "drift", "top_k": 8}
        return httpx.Response(200, json=body)

    # Act
    code, out, _err = _run(["kb", "search", "停电处置", "--mode", "drift"], handler, clean_env)
    # Assert
    assert code == EXIT_OK
    assert "命中 1 条" in out
    assert "mode=local" in out
    assert "degraded=true" in out
    assert "vector_unavailable" in out
    assert "单相接地故障处置流程" in out


def test_memory_context_渲染分层计数(clean_env: pytest.MonkeyPatch):
    # Arrange：MemoryContextOut 形态（api/01 §6.6）
    body = {"l1": {"window": [{"seq": 1}, {"seq": 2}], "blocks": []}, "l2": [{"fact_id": "f1"}], "l3": [], "l4": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["session_id"] == "s-1"
        assert request.url.params["mode"] == "light"
        return httpx.Response(200, json=body)

    # Act
    code, out, _err = _run(["memory", "context", "--session", "s-1", "--mode", "light"], handler, clean_env)
    # Assert
    assert code == EXIT_OK
    assert "L1 窗口 2 条" in out
    assert "L2 1 条" in out
    assert "mode=light" in out


def test_ontology_validate_不通过时退出码2并上传数据图(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange：Turtle 数据图文件 + 不符报告
    graph = tmp_path / "g.ttl"
    graph.write_text("<http://x/s> <http://x/p> <http://x/o> .", encoding="utf-8")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "conforms": False,
                "elapsed_ms": 387,
                "results": [
                    {"severity": "Violation", "constraint": "sh:in", "message": "状态不在枚举", "focus_node": "d1"}
                ],
            },
        )

    # Act
    code, out, _err = _run(["ontology", "validate", "0f2c", "--data-graph", str(graph)], handler, clean_env)
    # Assert：校验失败=退出码 2（CLI设计 §6）；data_graph 原文上传（按实现 OntologyValidateIn）
    assert code == EXIT_VALIDATION
    assert seen["path"] == "/api/v1/ontologies/0f2c/validate"
    assert seen["body"]["data_graph"].startswith("<http://x/s>")
    assert "违例 1 处" in out
    assert "sh:in" in out


def test_ontology_validate_通过时退出码0(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange
    graph = tmp_path / "g.ttl"
    graph.write_text("<http://x/s> <http://x/p> <http://x/o> .", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"conforms": True, "elapsed_ms": 20, "results": []})

    # Act
    code, out, _err = _run(["ontology", "validate", "0f2c", "--data-graph", str(graph)], handler, clean_env)
    # Assert
    assert code == EXIT_OK
    assert "[OK]" in out


def test_ontology_validate_数据图文件缺失时通用错误(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Act：不存在的文件路径
    code, _out, err = _run(
        ["ontology", "validate", "0f2c", "--data-graph", str(tmp_path / "nope.ttl")],
        lambda request: httpx.Response(200, json={}),  # pragma: no cover - 不应触网
        clean_env,
    )
    # Assert：本地错误不触网，退出码 1
    assert code == EXIT_GENERIC
    assert "无法读取" in err


# ================================================================ 退出码映射与配置注入


def test_退出码映射_网络3_鉴权4_禁止6_缺失5_校验2_其余1():
    # Act / Assert（CLI设计 §6）
    assert api_exit_code(ApiError("网络")) == EXIT_NETWORK
    assert api_exit_code(ApiError("过期", http_status=401)) == EXIT_AUTH
    assert api_exit_code(ApiError("越权", http_status=403)) == EXIT_FORBIDDEN
    assert api_exit_code(ApiError("无", http_status=404)) == EXIT_NOT_FOUND
    assert api_exit_code(ApiError("参数", http_status=422)) == EXIT_VALIDATION
    assert api_exit_code(ApiError("参数", http_status=400)) == EXIT_VALIDATION
    assert api_exit_code(ApiError("内部", http_status=500)) == EXIT_GENERIC


def test_退出码映射_4301归网络族():
    # Act / Assert：4301 SSE_REPLAY_EXPIRED 归流连接族（附重连指引）
    assert api_exit_code(ApiError("回放过期", http_status=410, code=4301)) == EXIT_NETWORK


def test_配置令牌注入_Bearer头随鉴权请求发送(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange：配置文件带令牌
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"auth": {"access_token": "tok-7"}}), encoding="utf-8")
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"items": [], "offset": 0, "limit": 20})

    # Act
    code, out, _err = _run(["sessions", "list", "--limit", "20"], handler, clean_env, cfg_file)
    # Assert
    assert code == EXIT_OK
    assert seen["auth"] == "Bearer tok-7"
    assert "共 0 条" in out


def test_错误体透出_code_message_trace_id进stderr(clean_env: pytest.MonkeyPatch):
    # Arrange：401 + 四字段错误体
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 1003, "message": "令牌已过期", "trace_id": "req_x1"})

    # Act
    code, _out, err = _run(["sessions", "list"], handler, clean_env)
    # Assert：code/message/trace_id 全部透出（api/01 §4.1）
    assert code == EXIT_AUTH
    assert "1003" in err
    assert "令牌已过期" in err
    assert "req_x1" in err


def test_无命令时打印帮助并返回通用错误码(clean_env: pytest.MonkeyPatch):
    # Act
    out, err = io.StringIO(), io.StringIO()
    code = main([], stdout=out, stderr=err, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    # Assert
    assert code == EXIT_GENERIC
    assert "usage" in out.getvalue()
