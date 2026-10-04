# tests/agent/test_model_failover.py
"""M4.5-C 模型 failover 深度（docs/Agent/12 §3 批次 C）验收测试。

- ①凭证池 RR 轮换与 429 冷却（伪 httpx MockTransport 造 429）+ 指数递增封顶；
- ②池空（全冷却）→ 最后一次原始错误透传；
- ③模型失败阈值 → 冷却 → fallback 链降级（llm.failover 事件，from/to/reason）；
- ④调用级重试「先落 llm.retry_scheduled 事件再退避等待」（DSH durable retry 顺序断言）
  + 成功落 llm.retry_succeeded（attempt 计数）；
- ⑤审计收口（G-1 后半）：韧性层在内、审计层在外——每次尝试各记一行审计且 trace 贯通；
  builtin 零 HTTP 直连（源码断言）、claude 无实装调用面（组合根未配 key）。

全量伪 provider / MockTransport / Fake 时钟，零真连零真 LLM；事件汇经 ContextVar 绑定
（与生产编排器同通道），事件断言读捕获列表。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.agent.business.adapters.builtin import BuiltinAdapter
from services.agent.business.chat_events import ChatCommand
from services.agent.business.chat_orchestrator import ChatOrchestrator
from services.gateway.app import _build_model_port
from services.platform.config import Settings
from services.platform.errors import tenant_id_ctx
from services.platform.llm.audited import AuditedModelPort
from services.platform.llm.cred_pool import CredentialPool, parse_api_keys
from services.platform.llm.events import _emitter, reset_llm_event_emitter, set_llm_event_emitter
from services.platform.llm.gateway import (
    ModelGatewayUnavailableError,
    OpenAICompatibleModelPort,
)
from services.platform.llm.resilience import FailoverModelPort, parse_fallback_chains

_TENANT = "00000000-0000-0000-0000-0000000000c3"
_TENANT_UUID = uuid.UUID(_TENANT)
_SCHEMA: dict[str, Any] = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
_ROOT = Path(__file__).resolve().parents[2]


class _FakeClock:
    """可拨单调时钟（凭证/模型冷却与退避断言口；缺省时刻 1000.0 避免零值歧义）。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _OkPort:
    """伪 provider：恒返回确定文本（降级目标桩）。"""

    provider = "openai_compatible"

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls = 0

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self.calls += 1
        return f"ok-from-{self.model}"

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"ok": True}

    async def stream_complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> AsyncIterator[str]:
        yield f"ok-from-{self.model}"


class _AlwaysFailPort:
    """伪 provider：恒抛 5002 不可达（主模型冷却触发桩）。"""

    provider = "openai_compatible"

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls = 0

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self.calls += 1
        raise ModelGatewayUnavailableError(f"{self.model} down")

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        raise ModelGatewayUnavailableError(f"{self.model} down")

    async def stream_complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> AsyncIterator[str]:
        raise ModelGatewayUnavailableError(f"{self.model} down")
        yield  # pragma: no cover —— 使本函数成为生成器（永不执行）


class _ScriptedPort:
    """伪 provider：按脚本逐次「抛错或返回文本」（调用级重试顺序断言桩；调用进 journal）。"""

    provider = "openai_compatible"

    def __init__(self, model: str, script: list[Any], journal: list[tuple[str, Any]] | None = None) -> None:
        self.model = model
        self._script = list(script)
        self._journal = journal

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        if self._journal is not None:
            self._journal.append(("call", len([j for j in self._journal if j[0] == "call"]) + 1))
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return str(step)

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("本用例不触结构化面")


# ── ①凭证池：RR 轮换 + 429 冷却 ───────────────────────────────────────────


def test_凭证池RR轮换_单凭证429冷却后跳过_到期自愈():
    # Arrange：双凭证池 + 可拨时钟
    clock = _FakeClock()
    pool = CredentialPool(
        provider="openai_compatible", keys=["k1", "k2"], cooldown_s=60, max_cooldown_s=600, clock=clock
    )
    # Act + Assert：RR 顺序出队（含回绕）
    assert pool.acquire() == "k1"
    assert pool.acquire() == "k2"
    assert pool.acquire() == "k1"
    # Act：k1 遇 429 → 冷却
    pool.report_failure("k1")
    # Assert：轮换跳过冷却凭证（k2 连续出队）；冷却名单命中 k1
    assert pool.acquire() == "k2"
    assert pool.acquire() == "k2"
    assert pool.cooling_keys() == ["k1"]
    # Act + Assert：冷却到期自愈（时间判定，k1 重新可用）
    clock.advance(60)
    assert pool.cooling_keys() == []
    assert pool.acquire() == "k1"


def test_凭证冷却指数递增_封顶600s_成功清零连败():
    # Arrange：单凭证池（连败不致池空拒绝，专注冷却时长断言）
    clock = _FakeClock()
    pool = CredentialPool(provider="openai_compatible", keys=["k1"], cooldown_s=60, max_cooldown_s=600, clock=clock)
    entry = pool._by_key["k1"]  # noqa: SLF001 —— 测试读内部状态断言冷却时长
    # Act + Assert：连败 1~4 → 60/120/240/480 递增
    for _, expected in ((1, 60.0), (2, 120.0), (3, 240.0), (4, 480.0)):
        pool.report_failure("k1")
        assert entry.cooldown_until == pytest.approx(clock.now + expected)
        clock.advance(expected + 1)  # 到期解冻再败（连败累计，冷却重启）
    # Act + Assert：第 5 连败 → 960 封顶 600
    pool.report_failure("k1")
    assert entry.cooldown_until == pytest.approx(clock.now + 600.0)
    # Act：成功（拿到 200）→ 连败清零；再败从基值 60 重新起算
    pool.report_success("k1")
    clock.advance(601)
    pool.report_failure("k1")
    assert entry.cooldown_until == pytest.approx(clock.now + 60.0)


async def test_429响应_轮换下一凭证重试成功_原凭证进冷却():
    # Arrange：k1 恒 429、k2 恒 200 的伪 transport；双凭证池
    auths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers["Authorization"]
        auths.append(auth)
        if auth == "Bearer k1":
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    clock = _FakeClock()
    pool = CredentialPool(
        provider="openai_compatible", keys=["k1", "k2"], cooldown_s=60, max_cooldown_s=600, clock=clock
    )
    port = OpenAICompatibleModelPort(
        base_url="http://llm",
        api_key="k1",
        model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        credential_pool=pool,
    )
    # Act
    data = await port.complete_structured(system="s", user="u", json_schema=_SCHEMA)
    # Assert：调用方透明（拿到 200）；轮换轨迹 k1→k2；k1 进冷却、k2 成功清零
    assert data == {"ok": True}
    assert auths == ["Bearer k1", "Bearer k2"]
    assert pool.cooling_keys() == ["k1"]
    await port.aclose()


def test_凭证表解析_主key与逗号附加key去重保序():
    # Act + Assert
    assert parse_api_keys("k1", "k2, k3 ,k1,") == ["k1", "k2", "k3"]
    assert parse_api_keys(None, "") == []
    assert parse_api_keys("only", "") == ["only"]


# ── ②池空（全冷却）→ 最后一次原始错误透传 ─────────────────────────────────


async def test_全冷却_透传最后一次原始错误不换错型():
    # Arrange：双凭证全 429
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    clock = _FakeClock()
    pool = CredentialPool(
        provider="openai_compatible", keys=["k1", "k2"], cooldown_s=60, max_cooldown_s=600, clock=clock
    )
    port = OpenAICompatibleModelPort(
        base_url="http://llm",
        api_key="k1",
        model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        credential_pool=pool,
    )
    # Act + Assert：池空 → 抛最后一次原始 429 错误（5002 族原样透传，非静默换错型）
    with pytest.raises(ModelGatewayUnavailableError, match="模型服务返回 429"):
        await port.complete_structured(system="s", user="u", json_schema=_SCHEMA)
    # Assert：双凭证全部进入冷却
    assert sorted(pool.cooling_keys()) == ["k1", "k2"]
    await port.aclose()


# ── ③模型失败阈值 → 冷却 → fallback 链降级事件 ───────────────────────────


async def test_模型连续失败达阈值进冷却_冷却后按链降级_发llm_failover事件():
    # Arrange：主模型恒失败；降级模型可用；阈值 3；重试关闭（每调用恰 1 次尝试，连败按调用累计）
    events: list[tuple[str, dict[str, Any]]] = []

    async def emitter(event_type: str, data: dict[str, Any]) -> None:
        events.append((event_type, dict(data)))

    clock = _FakeClock()
    primary = _AlwaysFailPort("local-main")
    backup = _OkPort("local-backup")
    port = FailoverModelPort(
        primary,
        provider="openai_compatible",
        model="local-main",
        fallback_factory=lambda name: backup if name == "local-backup" else None,
        chains={"local-main": ("local-backup",)},
        fail_threshold=3,
        model_cooldown_s=120.0,
        retry_max_attempts=1,
        clock=clock,
    )
    token = set_llm_event_emitter(emitter)
    try:
        # Act：前三连败（1、2 次未达阈值；第 3 次进冷却）——均上抛
        for _ in range(3):
            with pytest.raises(ModelGatewayUnavailableError):
                await port.complete([{"role": "user", "content": "hi"}], trace_id="t-fo")
        # Assert：阈值 3 → 冷却 120s 生效
        assert primary.calls == 3
        clock.advance(1)  # 冷却窗口内（1001 < 1000+120）
        # Act：冷却中第 4 次调用 → 按链降级到 backup 成功
        answer = await port.complete([{"role": "user", "content": "hi"}], trace_id="t-fo")
        # Assert：降级成功 + llm.failover 事件（from/to/reason）恰发一次
        assert answer == "ok-from-local-backup"
        assert backup.calls == 1
        assert [name for name, _ in events] == ["llm.failover"]
        payload = events[0][1]
        assert payload["from"] == "local-main"
        assert payload["to"] == "local-backup"
        assert payload["reason"] == "model_cooldown"
        assert payload["provider"] == "openai_compatible"
    finally:
        reset_llm_event_emitter(token)


async def test_模型冷却但链空_照打主模型不放大为硬失败():
    # Arrange：阈值 1（首败即冷却）+ 无降级链
    clock = _FakeClock()
    primary = _AlwaysFailPort("solo")
    port = FailoverModelPort(
        primary,
        provider="openai_compatible",
        model="solo",
        chains={},
        fail_threshold=1,
        model_cooldown_s=120.0,
        retry_max_attempts=1,
        clock=clock,
    )
    # Act：首败进冷却；冷却中再次调用
    with pytest.raises(ModelGatewayUnavailableError):
        await port.complete([{"role": "user", "content": "hi"}])
    with pytest.raises(ModelGatewayUnavailableError):
        await port.complete([{"role": "user", "content": "hi"}])
    # Assert：冷却无替代=建议性——仍尝试主模型（两次都打到 primary），不放大为硬失败
    assert primary.calls == 2


def test_降级链解析_分号分隔多链_空串禁用():
    # Act + Assert
    assert parse_fallback_chains("local-main->local-backup") == {"local-main": ("local-backup",)}
    assert parse_fallback_chains("a->b->c;x->y") == {"a": ("b", "c"), "x": ("y",)}
    assert parse_fallback_chains("") == {}
    assert parse_fallback_chains("no-arrow") == {}


# ── ④调用级重试：先落事件再退避等待（DSH durable retry 顺序）─────────────


async def test_重试先落retry_scheduled事件再退避等待_成功落retry_succeeded():
    # Arrange：首败次成脚本 + 共享时序日志（事件汇/退避睡眠/调用三方共同追加）
    journal: list[tuple[str, Any]] = []

    async def emitter(event_type: str, data: dict[str, Any]) -> None:
        journal.append(("event", (event_type, dict(data))))

    async def sleeper(seconds: float) -> None:
        journal.append(("sleep", seconds))

    scripted = _ScriptedPort(
        "m",
        [ModelGatewayUnavailableError("boom"), "ok-text"],
        journal=journal,
    )
    clock = _FakeClock()
    port = FailoverModelPort(
        scripted,
        provider="openai_compatible",
        model="m",
        retry_max_attempts=2,
        retry_backoff_ms=200,
        clock=clock,
        sleeper=sleeper,
    )
    token = set_llm_event_emitter(emitter)
    try:
        # Act
        answer = await port.complete([{"role": "user", "content": "hi"}], trace_id="t-retry")
    finally:
        reset_llm_event_emitter(token)
    # Assert：顺序=调用1 → 落 retry_scheduled → 退避 → 调用2 → 落 retry_succeeded
    # （先落事件再等待：进程在退避中崩溃，重试意图已持久化——DSH durable retry 语义）
    assert [entry[0] for entry in journal] == ["call", "event", "sleep", "call", "event"]
    scheduled = journal[1][1]
    assert scheduled[0] == "llm.retry_scheduled"
    assert scheduled[1]["attempt"] == 2  # 调度的下一次尝试序次
    assert scheduled[1]["backoff_ms"] == 200
    assert scheduled[1]["provider"] == "openai_compatible"
    assert scheduled[1]["model"] == "m"
    assert scheduled[1]["trace_id"] == "t-retry"
    succeeded = journal[4][1]
    assert succeeded[0] == "llm.retry_succeeded"
    assert succeeded[1]["attempt"] == 2  # 成功尝试计数
    assert succeeded[1]["model"] == "m"
    # Assert：重试后拿到成功结果
    assert answer == "ok-text"


async def test_重试耗尽_上抛原始错误_不落retry_succeeded():
    # Arrange：两次尝试全失败（retry_max_attempts=2）
    events: list[tuple[str, dict[str, Any]]] = []

    async def emitter(event_type: str, data: dict[str, Any]) -> None:
        events.append((event_type, dict(data)))

    port = FailoverModelPort(
        _AlwaysFailPort("m"),
        provider="openai_compatible",
        model="m",
        retry_max_attempts=2,
        retry_backoff_ms=0,
        clock=_FakeClock(),
        sleeper=_NoopSleeper(),
    )
    token = set_llm_event_emitter(emitter)
    try:
        # Act + Assert：耗尽上抛
        with pytest.raises(ModelGatewayUnavailableError, match="m down"):
            await port.complete([{"role": "user", "content": "hi"}])
    finally:
        reset_llm_event_emitter(token)
    # Assert：仅首次失败落 retry_scheduled（末次失败无后续调度）；无 succeeded
    assert [name for name, _ in events] == ["llm.retry_scheduled"]
    assert events[0][1]["attempt"] == 2


class _NoopSleeper:
    """零退避睡眠桩（耗时断言不属本用例）。"""

    async def __call__(self, seconds: float) -> None:
        return None


# ── ⑤审计收口（G-1 后半）：韧性层在内、审计层在外 ─────────────────────────


def _nop_audit_buffer() -> Any:
    """审计缓冲（不触发 flush 的空工厂；本文件自含，防跨文件导入）。"""
    from services.platform.llm.audit import LlmCallAuditBuffer

    class _NopFactory:
        def __call__(self):  # noqa: ANN201
            raise AssertionError("本用例不触发 flush")

    return LlmCallAuditBuffer(_NopFactory(), max_batch=100, flush_interval_s=1.0, clock=lambda: 0.0)


def _fail_then_ok_client(captured: list[str]) -> httpx.AsyncClient:
    """MockTransport：首次 500（→5002 瞬时错误），重试后 200 合规输出（不触网）。"""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        captured.append(request.headers.get("Authorization", ""))
        if attempts["n"] == 1:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_韧性层在内审计层在外_每次尝试各记一行审计且trace贯通():
    # Arrange：完整装配序 Failover(Audited(OpenAI))——与组合根 _build_model_port 同构
    captured: list[str] = []
    buffer = _nop_audit_buffer()
    port = FailoverModelPort(
        AuditedModelPort(
            OpenAICompatibleModelPort(
                base_url="http://llm", api_key="k", model="m", client=_fail_then_ok_client(captured)
            ),
            buffer,
        ),
        provider="openai_compatible",
        model="m",
        retry_max_attempts=2,
        retry_backoff_ms=0,
        clock=_FakeClock(),
        sleeper=_NoopSleeper(),
    )
    tenant_token = tenant_id_ctx.set(_TENANT)
    try:
        # Act：首尝 500 → 调用级重试 1 次 → 成功
        data = await port.complete_structured(system="s", user="u", json_schema=_SCHEMA, trace_id="t-audit")
    finally:
        tenant_id_ctx.reset(tenant_token)
    # Assert：成功拿到结果；每次尝试各记一行审计（error+ok），trace_id 贯通两行
    assert data == {"ok": True}
    assert len(captured) == 2
    records = list(buffer._queue)  # noqa: SLF001 —— 测试读取缓冲内部断言字段
    assert [(r.status, r.trace_id, r.model) for r in records] == [("error", "t-audit", "m"), ("ok", "t-audit", "m")]
    # Assert：审计缓冲持有口对组合根可见（lifespan flush 循环 getattr("audit") 面）
    assert port.audit is buffer


def test_builtin零HTTP直连_模型请求点trace贯通_源码断言():
    # Arrange：builtin/claude 适配器与组合根源码（G-1 收口盘点口径）
    builtin_src = (_ROOT / "services/agent/business/adapters/builtin.py").read_text(encoding="utf-8")
    orchestrator_src = (_ROOT / "services/agent/business/chat_orchestrator.py").read_text(encoding="utf-8")
    # Assert：builtin 无 httpx import（直连禁止）——模型 IO 全走注入 ModelPort（组合根恒为 Audited 包裹面）
    assert "import httpx" not in builtin_src
    # Assert：builtin 两处模型请求点（真流式 + 伪流式回退）均贯通 trace_id
    assert builtin_src.count("trace_id=ctx.trace_id") == 2
    # Assert：claude 组合根未配 api_key（注册成功调用 5002）——当前无实装调用面，
    # 审计收口最小面=builtin（claude 直连审计随其获得实装 key 的批次收口）
    assert 'adapters["claude"] = claude_adapter or ClaudeAdapter()' in orchestrator_src
    assert "ClaudeAdapter(api_key" not in orchestrator_src


def test_多凭证配置_组合根建池注入_单凭证不建池():
    # Arrange：显式 Settings 压过本机 env（防串扰）
    multi = Settings(llm_base_url="http://x/v1", llm_api_key="k1", llm_api_keys_extra="k2, k3")
    single = Settings(llm_base_url="http://x/v1", llm_api_key="k1", llm_api_keys_extra="")
    # Act
    port_multi = _build_model_port(multi)
    port_single = _build_model_port(single)
    # Assert：多凭证 → 池 3 凭证、客户端不固化 Bearer（逐请求携带）；单凭证 → 不建池（零行为变化）
    assert isinstance(port_multi, FailoverModelPort)
    channel_multi = port_multi._inner._inner
    assert isinstance(channel_multi, OpenAICompatibleModelPort)
    pool = channel_multi._credential_pool
    assert pool is not None and pool.size == 3
    assert channel_multi._client.headers.get("Authorization") is None
    channel_single = port_single._inner._inner
    assert channel_single._credential_pool is None
    assert channel_single._client.headers["Authorization"] == "Bearer k1"


# ── 事件通道生产接线：编排器逐 Run 绑定 → emitter（先落库后推送）──────────


class _ProbeModel:
    """ModelPort 桩：在模型请求点探测 ContextVar 绑定窗口（Run 内已绑定/Run 后解绑）。

    只备结构化面（无 stream_complete）→ builtin 走伪流式回退路径，请求点=complete_structured。
    """

    provider = "openai_compatible"

    def __init__(self) -> None:
        self.bound_during_call: list[bool] = []

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.bound_during_call.append(_emitter.get() is not None)
        return {"answer": "ok"}

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        return "ok"


class _StubL1:
    """L1 存储桩（编排器组装上下文面，行为无关本用例）。"""

    async def append_window_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def read(self, *args: Any, **kwargs: Any) -> None:
        return None


class _StubAssembler:
    """组装器桩：恒降级空上下文（记忆/检索面无关本用例）。"""

    def __init__(self) -> None:
        self.l1 = _StubL1()

    async def append_window_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def assemble(self, **kwargs: Any) -> Any:
        from services.agent.business.chat_context import ChatContext

        return ChatContext(degraded=True)


class _StubUow:
    """UoW 桩：for_tenant 短事务 → tx.tasks.append_event 记录（先落库断言口）。

    fail_on 非空时，命中事件名的落库抛错（容错路径断言口）。
    """

    def __init__(self, log: list[tuple[str, str]], *, fail_on: str | None = None) -> None:
        self._log = log
        self._fail_on = fail_on

    @asynccontextmanager
    async def for_tenant(self, tenant_id: uuid.UUID) -> AsyncIterator[Any]:
        yield _StubTx(self._log, fail_on=self._fail_on)  # noqa: SLF001 —— 桩内闭包传递


class _StubTx:
    def __init__(self, log: list[tuple[str, str]], *, fail_on: str | None = None) -> None:
        self.tasks = self
        self._log = log
        self._fail_on = fail_on

    async def append_event(self, task_id: uuid.UUID, event: Any) -> int:
        if self._fail_on is not None and event.event_type == self._fail_on:
            raise RuntimeError("db down")
        self._log.append(("db", event.event_type))
        return 1


class _StubHub:
    """hub 桩：同步二元组形态（进程内 publish 同款；推送顺序断言口）。"""

    def __init__(self, log: list[tuple[str, str]]) -> None:
        self._log = log

    def publish(self, session_id: uuid.UUID, name: str, data: dict[str, Any]) -> tuple[int, bytes]:
        self._log.append(("hub", name))
        return 1, b"frame"


async def test_编排器Run生命周期内绑定事件汇_Run外解绑():
    # Arrange：模型桩在请求点探测绑定窗口；事件汇工厂捕获绑定命令
    probe = _ProbeModel()
    factory_calls: list[ChatCommand] = []

    def factory(command: ChatCommand) -> Any:
        factory_calls.append(command)

        async def emit(event_type: str, data: dict[str, Any]) -> None:
            return None

        return emit

    orchestrator = ChatOrchestrator(
        adapters={"builtin": BuiltinAdapter(probe)},
        assembler=_StubAssembler(),  # type: ignore[arg-type]
        llm_event_emitter_factory=factory,
    )
    command = ChatCommand(
        tenant_id=_TENANT_UUID,
        user_id=_TENANT_UUID,
        session_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        message="hi",
        trace_id="t-bind",
        adapter="builtin",
    )
    # Act
    _ = [event async for event in orchestrator.stream_chat(command)]
    # Assert：工厂按 Run 绑定（恰一次、命中同命令）；模型请求点处于绑定窗口内；Run 结束解绑
    assert len(factory_calls) == 1 and factory_calls[0].task_id == command.task_id
    assert probe.bound_during_call == [True]
    assert _emitter.get() is None


async def test_llm事件汇工厂_先落库后推送_落库失败不阻断():
    # Arrange：UoW/hub 桩共享时序日志；第二个汇的落库对 retry_scheduled 事件抛错（容错路径）
    log: list[tuple[str, str]] = []
    hub = _StubHub(log)
    from services.agent.api.sessions import build_llm_event_emitter_factory

    command = ChatCommand(
        tenant_id=_TENANT_UUID,
        user_id=_TENANT_UUID,
        session_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        message="hi",
        trace_id="t-order",
        adapter="builtin",
    )
    emit_ok = build_llm_event_emitter_factory(_StubUow(log), hub)(command)
    emit_failing_db = build_llm_event_emitter_factory(_StubUow(log, fail_on="llm.retry_scheduled"), hub)(command)
    # Act：正常事件（先落库后推送）+ 落库抛错事件（emit 不上抛，推送照常）
    await emit_ok("llm.failover", {"from": "a", "to": "b", "reason": "model_cooldown"})
    await emit_failing_db("llm.retry_scheduled", {"attempt": 2, "backoff_ms": 200})
    # Assert：每事件均「先 db 后 hub」；落库失败只告警（无 db 行）不阻断推送
    assert log == [
        ("db", "llm.failover"),
        ("hub", "llm.failover"),
        ("hub", "llm.retry_scheduled"),
    ]
