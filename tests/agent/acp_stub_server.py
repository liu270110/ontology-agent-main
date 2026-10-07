# tests/agent/acp_stub_server.py
"""手写 stdio loop 假 ACP server（桩进程，docs/Agent/20 §2.3「python 假 ACP server 走 JSON-RPC stdio」）。

以子进程运行（`python acp_stub_server.py`），环境变量驱动剧本：
- ACP_STUB_SCENARIO=normal：initialize 握手 → session/new → prompt 流式 update（思考/文本/
  工具）→ stopReason=end_turn；
- ACP_STUB_SCENARIO=permission：prompt 先推 tool_call update，再发 session/request_permission
  （同步阻塞 RPC），等待 client 应答后把决议 kind 写进文本块（decision=<kind>）→ end_turn；
- ACP_STUB_SCENARIO=dead：spawn 即退出（探活失败路径）；
- ACP_STUB_SCENARIO=bad_init：initialize 应答缺 protocolVersion（握手校验失败路径）。
- ACP_STUB_LOG=<path>：入站报文逐行 JSON 追加（cancel 批量回 cancelled 的线面断言证据）。

单线程 stdin 循环（permission 剧本中 request_permission 挂起期间仍读 stdin——与 ACP 规范
「turn 中途同步阻塞 RPC、client 可随时 session/cancel」一致）。
"""

from __future__ import annotations

import json
import os
import sys

SCENARIO = os.environ.get("ACP_STUB_SCENARIO", "normal")
LOG_PATH = os.environ.get("ACP_STUB_LOG", "")

PERM_OPTIONS = [
    {"optionId": "opt_allow_once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "opt_allow_always", "name": "Always allow", "kind": "allow_always"},
    {"optionId": "opt_reject_once", "name": "Reject", "kind": "reject_once"},
]
_OPTION_KINDS = {o["optionId"]: o["kind"] for o in PERM_OPTIONS}

_session_counter = 0
_perm_counter = 9000
_pending_prompt: tuple | None = None  # (msg_id, session_id)
_pending_perm: tuple | None = None  # (msg_id, session_id)


def _log_in(msg: dict) -> None:
    if not LOG_PATH:
        return
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"dir": "in", "msg": msg}, ensure_ascii=False) + "\n")


def _write(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _send_update(sid: str, update: dict) -> None:
    _write({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": update}})


def _text(text: str) -> dict:
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def _respond_prompt(msg_id, stop_reason: str) -> None:
    _write({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": stop_reason}})


def _finish_perm(sid: str, decision_text: str) -> None:
    """决议回填后收尾：decision 文本块 + prompt end_turn。"""
    assert _pending_prompt is not None
    _send_update(sid, _text(decision_text))
    _respond_prompt(_pending_prompt[0], "end_turn")
    globals()["_pending_prompt"] = None
    globals()["_pending_perm"] = None


if SCENARIO == "dead":
    sys.exit(0)  # spawn 即死：探活/通道建立失败路径

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        msg = json.loads(raw)
    except ValueError:
        continue
    _log_in(msg)
    method = msg.get("method")
    mid = msg.get("id")

    if method is not None and mid is not None:  # 对端请求
        if method == "initialize":
            if SCENARIO == "bad_init":
                _write({"jsonrpc": "2.0", "id": mid, "result": {}})  # 缺 protocolVersion
            else:
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}},
                    }
                )
        elif method == "session/new":
            _session_counter += 1
            _write({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": f"sess_stub_{_session_counter}"}})
        elif method == "session/prompt":
            sid = (msg.get("params") or {}).get("sessionId") or ""
            _pending_prompt = (mid, sid)
            if SCENARIO == "permission":
                # 先推工具调用 update，再发同步阻塞权限请求（05 §2.2 ACP 线面）
                _send_update(
                    sid,
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc_1",
                        "title": "写文件",
                        "kind": "edit",
                        "status": "pending",
                    },
                )
                _perm_counter += 1
                _pending_perm = (_perm_counter, sid)
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": _perm_counter,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": sid,
                            "toolCall": {"toolCallId": "tc_1", "title": "写文件", "kind": "edit"},
                            "options": PERM_OPTIONS,
                            "kind": "edit",
                        },
                    }
                )
            else:  # normal
                _send_update(
                    sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "推理中"}}
                )
                _send_update(sid, _text("你好，"))
                _send_update(sid, _text("停电分析完成。"))
                _send_update(
                    sid,
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc_1",
                        "title": "查询工单",
                        "kind": "read",
                        "status": "completed",
                    },
                )
                _respond_prompt(mid, "end_turn")
                _pending_prompt = None
        else:
            _write({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"not supported: {method}"}})

    elif method is not None:  # 通知
        if method == "session/cancel":
            if _pending_perm is not None:
                _pending_perm = None  # client 侧负责对其请求回 cancelled（本桩只记录线面证据）
            if _pending_prompt is not None:
                _respond_prompt(_pending_prompt[0], "cancelled")
                _pending_prompt = None

    elif mid is not None and _pending_perm is not None and mid == _pending_perm[0]:
        # 权限应答回填（result.outcome.outcome=selected|cancelled）
        outcome = (msg.get("result") or {}).get("outcome") or {}
        kind = outcome.get("outcome")
        sid = _pending_perm[1]
        if kind == "selected":
            _finish_perm(sid, f"decision={_OPTION_KINDS.get(outcome.get('optionId'), 'unknown')}")
        else:
            _finish_perm(sid, "decision=cancelled")
