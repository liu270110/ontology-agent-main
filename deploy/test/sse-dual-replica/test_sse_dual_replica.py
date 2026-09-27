#!/usr/bin/env python
"""deploy/test/sse-dual-replica/test_sse_dual_replica.py — 批次 B-① 双副本 SSE 压测/验证脚本。

对 docs/api/02-SSE事件流协议.md 四个契约判定的实测（结果回填
docs/architecture/评审-2026-09-28-M0-M3阶段验收审核.md §4 批次 B-①）：

  ① 跨副本订阅（§5 多副本语义）：A 副本发消息产生事件流（写 Redis Stream），B 副本订阅者
     收到同一 seq 序列（写入与推送分离、网关无状态、任意副本可服务任意会话）；
  ② 断线重连（§4）：从 A 断开后携 Last-Event-ID 重连 B，回放 + 实时无缝衔接不重不漏
     （header 主通道 + ``?last_event_id=`` query 兜底通道均验）；
  ③ 回放窗口（§4）：last_event_id 早于窗口缺口 → HTTP 410 + code 4301 SSE_REPLAY_EXPIRED。
     默认经 Redis XTRIM 等效模拟 MAXLEN 裁剪（缺口判定与网关 XRANGE 快照走同一代码路径，
     见 services/gateway/sse/redis_hub.py subscribe）——秒级确定性；--full-window 改走
     真实 MAXLEN 1000 灌满裁剪（压测口径，分钟级）；
  ④ 心跳保活（§2）：空闲 15s 注释帧 ``: ping``（不计入事件序列）。

依赖：标准库 + httpx + redis（pyproject dependencies 已含，无新增）。用法见同目录 README.md。
退出码：任一判定 FAIL → 1；全部 PASS → 0。帧格式契约=api/02 §2（id/event/data 三行 + 空行结尾）。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import sys
import time
import uuid
from dataclasses import dataclass

import httpx
import redis

if sys.platform == "win32":  # 与 services/gateway/app.py 同款：Selector 循环（Windows 兼容纪律）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 种子常量（m1_seed_roles_and_admin 迁移；--jwt-secret 直签兜底时用作 claims 身份）
_SEED_ADMIN_USER_ID = "01a0de95-a52b-7e73-97c8-f73d8d5223d8"
_SEED_TENANT_DEFAULT = "01a0de95-a52a-74b0-bc74-ca4115471ed9"
_JWT_ISSUER = "ontology-agent"  # services/platform/security.py ISSUER/AUDIENCE/ALGORITHM
_JWT_AUDIENCE = "ontology-agent-api"

_TERMINAL_EVENTS = {"RUN_FINISHED", "RUN_ERROR"}  # api/02 §3 终态事件（终态后不再有任何事件）
_STREAM_KEY = "sse:stream:{sid}"  # services/gateway/sse/redis_hub.py 同款 key
_SEQ_KEY = "sse:seq:{sid}"
_PREFIX = "/api/v1"


# ── SSE 帧解析（api/02 §2 帧格式）─────────────────────────────────────────────


@dataclass
class SseFrame:
    """一帧 SSE：事件帧（id/event/data）或注释帧（心跳，不计入事件序列）。"""

    id: int | None
    event: str | None
    data: str
    is_comment: bool

    @property
    def is_terminal(self) -> bool:
        return self.event in _TERMINAL_EVENTS


class SseParser:
    """增量解析器：喂原始字节块，按空行分帧（兼容 \r\n）；跨 chunk 缓冲。"""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: bytes) -> list[SseFrame]:
        self._buf += chunk.decode("utf-8", errors="replace")
        frames: list[SseFrame] = []
        while "\n\n" in self._buf:
            raw, self._buf = self._buf.split("\n\n", 1)
            frame = self._parse_block(raw)
            if frame is not None:
                frames.append(frame)
        return frames

    @staticmethod
    def _parse_block(raw: str) -> SseFrame | None:
        lines = [line for line in raw.replace("\r\n", "\n").split("\n") if line]
        if not lines:
            return None
        fid: int | None = None
        event: str | None = None
        data = ""
        comments: list[str] = []
        for line in lines:
            if line.startswith(":"):
                comments.append(line[1:].strip())  # api/02 §2：注释帧（心跳）不计入事件序列
            elif line.startswith("id:"):
                value = line[len("id:") :].strip()
                fid = int(value) if value.isdigit() else None
            elif line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        if comments:
            return SseFrame(id=None, event=None, data=" ".join(comments), is_comment=True)
        return SseFrame(id=fid, event=event, data=data, is_comment=False)


def _seqs(frames: list[SseFrame]) -> list[int]:
    return [f.id for f in frames if not f.is_comment]


def _names(frames: list[SseFrame]) -> list[str]:
    return [f.event or "" for f in frames if not f.is_comment]


# ── HTTP/SSE 收发 ────────────────────────────────────────────────────────────


async def collect_sse(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    last_event_id: int | None = None,
    use_query_token: bool = False,
    stop_on_terminal: bool = True,
    quiet_s: float = 2.5,
    deadline_s: float = 90.0,
    sink: list[SseFrame] | None = None,
) -> list[SseFrame]:
    """打开一条 SSE 流收集帧，返回事件帧列表（注释帧保留在 sink）。

    退出条件：终态事件（stop_on_terminal）/ 静默 quiet_s（借 httpx read 超时实现，读超时
    阈值=quiet_s+2，须小于心跳周期 15s）/ 总 deadline_s。sink 提供时帧实时追加（外部可于
    任务被取消后仍保留已收帧）。
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    params: dict[str, str] = {}
    if last_event_id is not None:
        if use_query_token:  # api/02 §4：?last_event_id= 为 query 兜底通道
            params["last_event_id"] = str(last_event_id)
        else:
            headers["Last-Event-ID"] = str(last_event_id)
    timeout = httpx.Timeout(connect=5.0, read=quiet_s + 2.0, write=10.0, pool=5.0)

    async def _run() -> list[SseFrame]:
        frames: list[SseFrame] = sink if sink is not None else []
        parser = SseParser()
        request = client.build_request(method, url, headers=headers, params=params, json=body, timeout=timeout)
        response = await client.send(request, stream=True)
        try:
            if response.status_code != 200:
                text = (await response.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {response.status_code}: {text[:300]}")
            async for chunk in response.aiter_bytes():
                for frame in parser.feed(chunk):
                    frames.append(frame)
                    if stop_on_terminal and frame.is_terminal:
                        return [f for f in frames if not f.is_comment]
            return [f for f in frames if not f.is_comment]
        except httpx.ReadTimeout:  # 静默窗口到期=正常收尾
            return [f for f in frames if not f.is_comment]
        finally:
            await response.aclose()

    return await asyncio.wait_for(_run(), timeout=deadline_s)


async def post_message(client: httpx.AsyncClient, base: str, sid: str, token: str, content: str) -> list[SseFrame]:
    """POST messages（Accept: text/event-stream）→ 直接以 SSE 收完本轮事件（api/02 §1 端点一）。"""
    url = f"{base}{_PREFIX}/sessions/{sid}/messages"
    return await collect_sse(client, url, token, method="POST", body={"content": content}, quiet_s=5.0)


async def rest_json(client: httpx.AsyncClient, method: str, url: str, token: str, **kwargs: object) -> dict:
    response = await client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)  # type: ignore[arg-type]
    payload = response.json() if response.content else {}
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {url} -> HTTP {response.status_code}: {response.text[:300]}")
    return payload  # type: ignore[no-any-return]


# ── 令牌获取（登录主通道 + 直签兜底）──────────────────────────────────────────


def mint_token(secret: str, user_id: str, tenant_id: str, scopes: list[str]) -> str:
    """stdlib HS256 直签（claims 同 services/platform/security.py build_claims）——登录通道不可用时的兜底。"""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "roles": ["admin"],
        "scopes": scopes,
        "typ": "access",
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + 3600,
        "iss": _JWT_ISSUER,
        "aud": _JWT_AUDIENCE,
    }

    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=").decode()

    signing_input = f"{b64(header)}.{b64(payload)}"
    sig = (
        base64.urlsafe_b64encode(hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest())
        .rstrip(b"=")
        .decode()
    )
    return f"{signing_input}.{sig}"


async def obtain_token(client: httpx.AsyncClient, base: str, args: argparse.Namespace) -> str:
    """令牌获取次序：--token > --jwt-secret 直签 > login（api/01 §5.9 / api/02 §6 同一 JWT）。"""
    if args.token:
        return args.token
    if args.jwt_secret:
        print("[token] 使用 --jwt-secret 直签（scopes 含 session:write/session:chat，免种子角色授权）")
        return mint_token(
            args.jwt_secret,
            args.user_id,
            args.tenant_id,
            ["session:read", "session:write", "session:chat", "agent:read", "agent:write"],
        )
    payload = await rest_json(
        client, "POST", f"{base}{_PREFIX}/auth/login", "", json={"email": args.email, "password": args.password}
    )
    return payload["access_token"]  # type: ignore[no-any-return]


_SCOPE_HINT = (
    "[前置失败] 创建会话 403/2001 scope 不足：种子角色（m1_seed_roles_and_admin）不含 "
    "session:write/session:chat。二选一：\n"
    "  a) 授权后重新登录（scopes 登录时写入 claims）：\n"
    "     docker compose -f deploy/docker-compose.yml -f deploy/test/sse-dual-replica/"
    "docker-compose.sse-dual.yml exec postgres psql -U onto -d onto -c \\\n"
    "       \"UPDATE roles SET scopes = (SELECT array_agg(DISTINCT s) FROM unnest("
    "scopes || ARRAY['session:write','session:chat']::text[]) AS s) WHERE code = 'admin';\"\n"
    "  b) 脚本加 --jwt-secret dev-only-change-me 直签兜底（README「令牌获取」节）。"
)


# ── 四个判定 ─────────────────────────────────────────────────────────────────


async def judge_1_cross_replica(
    client: httpx.AsyncClient, args: argparse.Namespace, sid: str, token: str
) -> tuple[bool, str]:
    """① 跨副本订阅（api/02 §5）：A 发消息产生事件，B 收到同一 seq 序列。"""
    url_b = f"{args.gateway_b}{_PREFIX}/sessions/{sid}/events"
    sink_b: list[SseFrame] = []
    task_b = asyncio.create_task(
        collect_sse(
            client, url_b, token, stop_on_terminal=False, quiet_s=args.quiet_s, deadline_s=180.0, sink=sink_b
        )
    )
    await asyncio.sleep(1.0)  # B 订阅建流（快照空 → XREAD 游标 0-0 纯实时）
    frames_a = await post_message(client, args.gateway_a, sid, token, "B1-判定①-A副本消息")
    try:
        frames_b = await asyncio.wait_for(task_b, timeout=10.0)  # 静默收尾余量
    except TimeoutError:
        task_b.cancel()
        frames_b = [f for f in sink_b if not f.is_comment]
    seq_a, seq_b = _seqs(frames_a), _seqs(frames_b)
    if not seq_a:
        return False, f"A 侧未收到事件（seq_a={seq_a}）——检查网关日志/编排器"
    if seq_a != seq_b:
        return False, f"seq 序列不一致：A={seq_a} B={seq_b}"
    if _names(frames_a) != _names(frames_b):
        return False, f"事件名不一致：A={_names(frames_a)} B={_names(frames_b)}"
    if seq_b != list(range(seq_b[0], seq_b[0] + len(seq_b))):
        return False, f"B 侧 seq 非连续单调：{seq_b}"
    return True, f"A 侧 seq={seq_a} == B 副本实时收到 seq={seq_b}（同一序列，不重不漏）"


async def judge_2_reconnect(
    client: httpx.AsyncClient, args: argparse.Namespace, sid: str, token: str
) -> tuple[bool, str]:
    """② 断线重连（api/02 §4）：从 A 断开 → Last-Event-ID 重连 B，回放+实时不重不漏。"""
    url_a = f"{args.gateway_a}{_PREFIX}/sessions/{sid}/events"
    url_b = f"{args.gateway_b}{_PREFIX}/sessions/{sid}/events"
    # 阶段 1：A 副本在线订阅，收完一轮（在线期最后一帧 id = 断点 last_event_id）
    sink_a: list[SseFrame] = []
    task_a = asyncio.create_task(
        collect_sse(client, url_a, token, stop_on_terminal=False, quiet_s=args.quiet_s, deadline_s=180.0, sink=sink_a)
    )
    await asyncio.sleep(1.0)
    online = await post_message(client, args.gateway_b, sid, token, "B1-判定②-断线前一轮(B副本发布)")  # 跨副本发布
    await asyncio.wait_for(task_a, timeout=10.0)
    last_id = _seqs(online)[-1]
    if _seqs(sink_a) != _seqs(online):
        return False, f"断线前 A 订阅 seq={_seqs(sink_a)} != 发布 seq={_seqs(online)}"
    # 阶段 2：断开期间事件继续产生（订阅方离线，不消费）
    offline = await post_message(client, args.gateway_a, sid, token, "B1-判定②-断线期间一轮")
    expected = _seqs(offline)
    if not expected:
        return False, "断线期间未产生事件"
    # 阶段 3：携 Last-Event-ID 头重连 B 副本 → 恰好回放 (last_id, ...] 不重不漏
    replayed = await collect_sse(
        client, url_b, token, last_event_id=last_id, stop_on_terminal=False, quiet_s=args.quiet_s, deadline_s=60.0
    )
    if _seqs(replayed) != expected:
        return False, f"重连回放 seq={_seqs(replayed)} != 断线期间事件 {expected}（last_id={last_id}）"
    # 阶段 4：query 兜底通道同位点重连（回放只读不烧）
    replayed_q = await collect_sse(
        client, url_a, token, last_event_id=last_id, use_query_token=True, stop_on_terminal=False,
        quiet_s=args.quiet_s, deadline_s=60.0,
    )
    if _seqs(replayed_q) != expected:
        return False, f"?last_event_id= 兜底回放 seq={_seqs(replayed_q)} != {expected}"
    # 阶段 5：同一重连连接上回放→实时无缝衔接（回放后跨副本再发布一轮，原连接续收）
    sink_live: list[SseFrame] = []
    task_live = asyncio.create_task(
        collect_sse(
            client, url_b, token, last_event_id=last_id, stop_on_terminal=False,
            quiet_s=10.0, deadline_s=90.0, sink=sink_live,
        )
    )
    await asyncio.sleep(1.5)  # 回放（XRANGE 快照）先落
    live = await post_message(client, args.gateway_a, sid, token, "B1-判定②-重连后实时一轮")
    await asyncio.wait_for(task_live, timeout=30.0)
    combined = _seqs(sink_live)
    if combined != expected + _seqs(live):
        return False, f"回放→实时衔接 seq={combined} != {expected}+{_seqs(live)}"
    return True, (
        f"断点 last_event_id={last_id}：A 断 → B 重连回放 {expected} 不重不漏；"
        f"query 兜底同结果；同连接续收实时 {_seqs(live)}"
    )


async def judge_3_replay_window(
    client: httpx.AsyncClient, args: argparse.Namespace, sid: str, token: str, rds: redis.Redis
) -> tuple[bool, str]:
    """③ 回放窗口（api/02 §4）：last_event_id 早于窗口 → HTTP 410 + code 4301；窗内仍可订阅。"""
    url_b = f"{args.gateway_b}{_PREFIX}/sessions/{sid}/events"
    stream_key = _STREAM_KEY.format(sid=sid)
    # 缺口构造：两轮发布使 last_event_id 与窗口最旧条目拉开 ≥2（缺口判定=oldest-1 > last_event_id）
    final = await post_message(client, args.gateway_a, sid, token, "B1-判定③-超窗位点轮")
    stale_id = _seqs(final)[-1]
    extra = await post_message(client, args.gateway_a, sid, token, "B1-判定③-缺口拉开轮")
    if _seqs(extra)[-1] - stale_id < 2:
        return False, f"事件间隔不足（latest={_seqs(extra)[-1]}, stale={stale_id}），无法构造缺口"
    if args.full_window:
        # 真实压测口径：持续发布直至 MAXLEN 1000 裁剪把 stale_id 挤出窗口
        rounds = 0
        for i in range(args.soak_max_messages):
            rounds = i + 1
            await post_message(client, args.gateway_a, sid, token, f"B1-判定③-灌窗第{rounds}轮")
            first = rds.xrange(stream_key, min="-", max="+", count=1)
            if first and int(first[0][1]["seq"]) > stale_id + 1:
                break
        else:
            return False, f"灌满 {args.soak_max_messages} 轮仍未裁出缺口（Stream MAXLEN 未生效？）"
        how = f"真实 MAXLEN 灌满裁剪（{rounds} 轮）"
    else:
        # 等效口径：XTRIM 只留最新 1 条——网关侧重连走同一 XRANGE 快照缺口判定代码路径
        rds.xtrim(stream_key, maxlen=1, approximate=False)
        how = "XTRIM 等效裁剪（快照缺口判定同路径）"
    latest_entry = rds.xrevrange(stream_key, count=1)
    if not latest_entry:
        return False, "裁剪后 Stream 为空（XTRIM/灌窗未生效？）"
    latest_id = int(latest_entry[0][1]["seq"])
    # 超窗重连：Last-Event-ID 头 → 期望 4301（HTTP 410，建流前同步抛出→统一错误体）
    response = await client.get(
        url_b, headers={"Authorization": f"Bearer {token}", "Last-Event-ID": str(stale_id)}
    )
    body = response.json()
    if response.status_code != 410:
        return False, f"超窗重连期望 HTTP 410，实得 {response.status_code}（body={body}）"
    if int(body.get("code", 0)) != 4301:
        return False, f"错误码期望 4301 SSE_REPLAY_EXPIRED，实得 {body.get('code')}"
    # 对照：窗内（最新 seq）仍可正常订阅——只有超窗才 410
    edge_headers = {"Authorization": f"Bearer {token}", "Last-Event-ID": str(latest_id)}
    edge = await client.send(client.build_request("GET", url_b, headers=edge_headers), stream=True)
    edge_ok = edge.status_code == 200 and "text/event-stream" in edge.headers.get("content-type", "")
    await edge.aclose()
    if not edge_ok:
        return False, f"窗内（latest={latest_id}）订阅异常：HTTP {edge.status_code}"
    return True, f"{how}：last_event_id={stale_id} < 窗口 oldest-1={latest_id - 1} → HTTP 410 + code 4301；窗内订阅 200"


async def judge_4_heartbeat(
    client: httpx.AsyncClient, args: argparse.Namespace, sid: str, token: str
) -> tuple[bool, str]:
    """④ 心跳保活（api/02 §2）：空闲 {heartbeat}s 注释帧 ``: ping``（不计入事件序列）。"""
    url_a = f"{args.gateway_a}{_PREFIX}/sessions/{sid}/events"
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    # read 超时 > 心跳周期：ping 持续重置静默计时；收到 2 个心跳即证周期性保活，提前收尾
    timeout = httpx.Timeout(connect=5.0, read=args.heartbeat_s + 8.0, write=10.0, pool=5.0)
    parser = SseParser()
    comments: list[SseFrame] = []
    events: list[SseFrame] = []
    start = time.monotonic()
    async with client.stream("GET", url_a, headers=headers, timeout=timeout) as response:
        if response.status_code != 200:
            return False, f"订阅 HTTP {response.status_code}"
        try:
            async for chunk in response.aiter_bytes():
                for frame in parser.feed(chunk):
                    (comments if frame.is_comment else events).append(frame)
                if len(comments) >= 2:
                    break
        except httpx.ReadTimeout:
            pass
    elapsed = time.monotonic() - start
    if not comments:
        return False, f"{args.heartbeat_s}s 空闲窗口（实等 {elapsed:.0f}s）未收到注释帧（心跳缺失）"
    if events:
        return False, f"空闲期出现事件帧（应为纯心跳）：{[(f.id, f.event) for f in events]}"
    if not all("ping" in comment.data for comment in comments):
        return False, f"注释帧内容非 : ping：{[c.data for c in comments]}"
    return True, (
        f"空闲 {elapsed:.0f}s 收到 {len(comments)} 条注释帧「: ping」"
        f"（间隔≈{args.heartbeat_s}s，不计入事件序列）；事件帧数=0"
    )


# ── 主流程 ───────────────────────────────────────────────────────────────────


async def main() -> int:
    args = _parse_args()
    results: list[tuple[str, bool, str]] = []
    rds = redis.Redis.from_url(args.redis_url, decode_responses=True)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)) as client:
            # 前置：两副本存活 + 令牌 + 会话（403 时给出可执行的授权指引）
            for name, base in (("A", args.gateway_a), ("B", args.gateway_b)):
                probe = await client.get(f"{base}{_PREFIX}/healthz")
                if probe.status_code != 200:
                    print(f"[前置失败] 网关{name}（{base}）healthz -> {probe.status_code}；先 up compose 栈")
                    return 2
            token = await obtain_token(client, args.gateway_a, args)
            agent = await rest_json(
                client, "POST", f"{args.gateway_a}{_PREFIX}/agents", token,
                json={"name": f"sse-dual-{uuid.uuid4().hex[:8]}", "agent_tool": "builtin"},
            )
            try:
                session = await rest_json(
                    client, "POST", f"{args.gateway_a}{_PREFIX}/sessions", token, json={"agent_id": agent["id"]}
                )
            except RuntimeError as exc:
                if "403" in str(exc):
                    print(_SCOPE_HINT)
                    return 2
                raise
            sid = session["id"]
            print(f"[前置] 副本A={args.gateway_a} 副本B={args.gateway_b} session={sid}")

            for label, judge in (
                ("① 跨副本订阅", lambda: judge_1_cross_replica(client, args, sid, token)),
                ("② 断线重连", lambda: judge_2_reconnect(client, args, sid, token)),
                ("③ 回放窗口", lambda: judge_3_replay_window(client, args, sid, token, rds)),
            ):
                try:
                    ok, detail = await judge()
                except Exception as exc:  # noqa: BLE001 ——单判定异常不拖垮整场，记 FAIL 继续
                    ok, detail = False, f"{type(exc).__name__}: {exc}"
                results.append((label, ok, detail))
                print(f"[{'PASS' if ok else 'FAIL'}] {label}: {detail}")

            session4 = await rest_json(
                client, "POST", f"{args.gateway_a}{_PREFIX}/sessions", token, json={"agent_id": agent["id"]}
            )
            try:
                ok, detail = await judge_4_heartbeat(client, args, session4["id"], token)
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results.append(("④ 心跳保活", ok, detail))
            print(f"[{'PASS' if ok else 'FAIL'}] ④ 心跳保活: {detail}")

            # 清理：SSE Stream/seq 键（会话与 agent 作为压测留痕保留，TTL 自然过期）
            try:
                keys = [_STREAM_KEY.format(sid=sid), _SEQ_KEY.format(sid=sid)]
                keys += [_STREAM_KEY.format(sid=session4["id"]), _SEQ_KEY.format(sid=session4["id"])]
                rds.delete(*keys)
            except redis.RedisError as exc:
                print(f"[清理] Redis 键清理失败（不影响判定）: {exc}")
    finally:
        rds.aclose()

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n=== 批次 B-① 双副本 SSE 压测结果（api/02 §4/§5）===")
    for label, ok, detail in results:
        print(f"  {label}  {'PASS' if ok else 'FAIL'}  - {detail}")
    print(f"结论：{passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批次 B-① 双副本 SSE 压测/验证（api/02 §4/§5 四判定）")
    parser.add_argument("--gateway-a", default="http://127.0.0.1:8364", help="副本 A 基址")
    parser.add_argument("--gateway-b", default="http://127.0.0.1:8365", help="副本 B 基址")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6379/0", help="Redis（判定③裁剪与清理用）")
    parser.add_argument("--email", default="admin@local", help="登录邮箱（种子 admin）")
    parser.add_argument("--password", default="ChangeMe@FirstLogin", help="登录密码（种子占位口令）")
    parser.add_argument("--token", default=None, help="已有访问令牌（跳过登录）")
    parser.add_argument("--jwt-secret", default=None, help="直签兜底：OA_JWT_SECRET（免种子角色授权）")
    parser.add_argument("--user-id", default=_SEED_ADMIN_USER_ID, help="直签 claims sub（默认种子 admin）")
    parser.add_argument("--tenant-id", default=_SEED_TENANT_DEFAULT, help="直签 claims tenant_id（默认种子租户）")
    parser.add_argument("--heartbeat-s", type=int, default=15, help="预期心跳周期（=OA_SSE_HEARTBEAT_SECONDS）")
    parser.add_argument("--quiet-s", type=float, default=2.5, help="流收集静默窗（须 < 心跳周期）")
    parser.add_argument("--full-window", action="store_true", help="判定③走真实 MAXLEN 1000 灌满裁剪（分钟级）")
    parser.add_argument("--soak-max-messages", type=int, default=600, help="--full-window 灌窗轮数上限")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
