# tests/agent/test_cap_web.py
"""web 能力测试（docs/Agent/06 #3：web_fetch/web_search）。

纪律：全量 httpx.MockTransport / Fake 后端，**零真连**；AAA + 中文命名；
正反例覆盖：白名单拒绝（fail-closed）/重定向跨白名单/超时/超大截断/spill/HTML 抽取/
审计行（禁正文）/搜索端点判定与条目过滤/DDG 轻适配解析/绑定工厂形状。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from conftest import make_ctx

from services.agent.business.capabilities.web import (
    BODY_PREVIEW_CHARS,
    DEFAULT_TIMEOUT_S,
    WEB_FETCH_ACTION_IRI,
    WEB_SEARCH_ACTION_IRI,
    EgressAllowlist,
    WebSearchTool,
    build_web_bindings,
    extract_html_text,
)
from services.agent.business.capabilities.web.search import DuckDuckGoHtmlSearch, SearchHit
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.extensions import ToolPort
from services.agent.business.kernel.spill import SpillStore
from services.agent.domain.model.kernel_actions import ToolCall
from services.agent.domain.model.kernel_context import TenantContext, TrustLevel
from services.platform.errors import ErrorCode

# ── 夹具与桩 ─────────────────────────────────────────────────────────────

DDG_HTML = """
<html><head><title>DuckDuckGo Search</title></head><body>
<div class="result">
  <h2 class="result__title"><a class="result__a"
    href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.com%2Fguide&amp;rut=abc">停电分析指南</a></h2>
  <a class="result__snippet" href="https://docs.example.com/guide">电网停电原因分析实操指南</a>
</div>
<div class="result">
  <h2 class="result__title"><a class="result__a" href="https://blog.other.org/post">直链博文</a></h2>
  <a class="result__snippet" href="https://blog.other.org/post">另一条摘要</a>
</div>
</body></html>
"""


def make_call(action_iri: str, params: dict[str, Any]) -> ToolCall:
    return ToolCall(action_iri=action_iri, parameters=params, param_hash="0" * 64)


class SpyTransport(httpx.MockTransport):
    """MockTransport + 按主机请求计数（零外呼断言口；async 客户端走 handle_async_request）。"""

    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.requests_by_host: dict[str, int] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or "?"
        self.requests_by_host[host] = self.requests_by_host.get(host, 0) + 1
        return await super().handle_async_request(request)


def spy_factory(transport: httpx.MockTransport, *, timeouts: list[float] | None = None):
    """客户端工厂桩：记录每次注入的超时秒（超时必设断言口）并回 MockTransport 客户端。"""

    def factory(timeout_s: float) -> httpx.AsyncClient:
        if timeouts is not None:
            timeouts.append(timeout_s)
        return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout_s), follow_redirects=False)

    return factory


class FakeSpillStore:
    """SpillStore 桩：记录 key/payload，可注入故障。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.payloads: dict[str, str] = {}

    async def put(self, key: str, payload: str) -> str:
        if self.fail:
            raise RuntimeError("对象存储不可用")
        self.payloads[key] = payload
        return f"mem://{key}"


class FakeSearch:
    """SearchBackend 内存桩（endpoint_host=None：纯逻辑，无外呼）。"""

    name = "fake_search"
    endpoint_host: str | None = None

    def __init__(self, hits: list[SearchHit] | None = None, *, error: Exception | None = None) -> None:
        self.hits = hits or []
        self.error = error
        self.calls: list[str] = []

    async def search(self, query: str, *, max_results: int, timeout_s: float) -> list[SearchHit]:
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return self.hits[:max_results]


def recording_audit() -> tuple[Any, list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []

    def sink(record: dict[str, Any]) -> None:
        records.append(record)

    return sink, records


async def invoke_tool(tool: Any, params: dict[str, Any], ctx: TenantContext, *, timeout_ms: int = 30_000):
    call = make_call(tool.meta.semantic_annotation["action_iri"], params)
    return await tool.invoke(call, ctx, timeout_ms=timeout_ms)


def _html_transport(body: str | bytes, *, content_type: str = "text/html; charset=utf-8") -> SpyTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    return SpyTransport(handler)


# ── web_fetch：参数与协议门 ──────────────────────────────────────────────


async def test_缺url参数_结构化拒绝3001():
    # arrange
    transport = SpyTransport(lambda req: httpx.Response(200, text="x"))
    tool, _ = build_web_bindings(("docs.example.com",), client_factory=spy_factory(transport))
    # act
    result = await invoke_tool(tool, {}, make_ctx())
    # assert
    assert result.ok is False and result.error_code == int(ErrorCode.PARAM_INVALID)
    assert transport.requests_by_host == {}  # 参数门拦截在前，零外呼


async def test_非http协议url_拒绝3001且零外呼():
    # arrange：file/ftp 等协议 SSRF 最小关断
    transport = SpyTransport(lambda req: httpx.Response(200, text="x"))
    tool, _ = build_web_bindings(("docs.example.com",), client_factory=spy_factory(transport))
    # act
    result = await invoke_tool(tool, {"url": "file:///etc/passwd"}, make_ctx())
    # assert
    assert result.ok is False and result.error_code == int(ErrorCode.PARAM_INVALID)
    assert transport.requests_by_host == {}


async def test_默认空白名单_failclosed_全拒且零外呼():
    # arrange：缺省空白名单（fail-closed 缺省）——出口白名单是安全边界而非功能开关
    transport = SpyTransport(lambda req: httpx.Response(200, text="x"))
    factory_calls: list[float] = []
    tool, _ = build_web_bindings((), client_factory=spy_factory(transport, timeouts=factory_calls))
    # act
    result = await invoke_tool(tool, {"url": "https://docs.example.com/a"}, make_ctx())
    # assert：全拒且连客户端都未装配（零外呼）
    assert result.ok is False and result.error_code == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert transport.requests_by_host == {} and factory_calls == []


async def test_非白名单域名_拒绝2001且零外呼():
    # arrange
    transport = SpyTransport(lambda req: httpx.Response(200, text="x"))
    tool, _ = build_web_bindings(("docs.example.com",), client_factory=spy_factory(transport))
    # act
    result = await invoke_tool(tool, {"url": "https://evil.example.org/page"}, make_ctx())
    # assert
    assert result.ok is False and result.error_code == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert "evil.example.org" in (result.error_message or "")
    assert transport.requests_by_host == {}


# ── web_fetch：正例 / 重定向 / 截断 / spill ─────────────────────────────


async def test_白名单内抓取成功_正文抽取与B3标界元数据齐备():
    # arrange
    tool, _ = build_web_bindings(
        ("docs.example.com",), client_factory=spy_factory(_html_transport("<p>停电结论：线路过载</p>"))
    )
    # act
    result = await invoke_tool(tool, {"url": "https://docs.example.com/report/1"}, make_ctx())
    # assert：正文 + B3 agent_attested 元数据（来源 URL/抓取时间/截断标记）
    assert result.ok is True
    output = result.output
    assert "线路过载" in output["text"]
    assert output["url"] == "https://docs.example.com/report/1"
    assert output["requested_url"] == output["url"]
    assert output["fetched_at"] and output["truncated"] is False
    assert result.trust_level is TrustLevel.AGENT_ATTESTED  # B3：外部输入不可信标界
    assert result.claimed_trust_level is None  # 工具不自称 externally_verified


async def test_重定向跨白名单_拒绝且目标站零请求():
    # arrange：合法站 302 → 非白名单站（跨白名单跳转在请求发出前拦截）
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "docs.example.com":
            return httpx.Response(302, headers={"location": "https://evil.example.org/steal"})
        return httpx.Response(200, text="不应到达")

    transport = SpyTransport(handler)
    tool, _ = build_web_bindings(("docs.example.com",), client_factory=spy_factory(transport))
    # act
    result = await invoke_tool(tool, {"url": "https://docs.example.com/redirect"}, make_ctx())
    # assert
    assert result.ok is False and result.error_code == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert transport.requests_by_host == {"docs.example.com": 1}  # evil 站零请求


async def test_重定向链内域名合法_跟随到最终正文():
    # arrange：白名单站内 302 → 200（相对 Location 解析；显式 text/html 走抽取分支）
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/jump":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(
            200,
            text="<html><head><title>最终页</title></head><body><p>到达</p></body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    tool, _ = build_web_bindings(("docs.example.com",), client_factory=spy_factory(SpyTransport(handler)))
    # act
    result = await invoke_tool(tool, {"url": "https://docs.example.com/jump"}, make_ctx())
    # assert
    assert result.ok is True
    assert result.output["url"].endswith("/final") and result.output["redirect_hops"] == 2
    assert result.output["title"] == "最终页" and "到达" in result.output["text"]


async def test_响应体超2MB上限_结构化失败5003():
    # arrange：2MB+1 字节（流式累计硬顶，防内存打爆）
    transport = _html_transport(b"x" * (2 * 1024 * 1024 + 1))
    tool, _ = build_web_bindings(("docs.example.com",), client_factory=spy_factory(transport))
    # act
    result = await invoke_tool(tool, {"url": "https://docs.example.com/huge"}, make_ctx())
    # assert
    assert result.ok is False and result.error_code == int(ErrorCode.MCP_TARGET_UNAVAILABLE)
    assert "上限" in (result.error_message or "")


async def test_正文超20KB_缺省内存截断并注明spill退化():
    # arrange：>20KB 正文且未注入 spill 存储（缺省内存截断并注明）
    body = "<p>" + "长" * (BODY_PREVIEW_CHARS + 500) + "</p>"
    tool, _ = build_web_bindings(("docs.example.com",), client_factory=spy_factory(_html_transport(body)))
    # act
    result = await invoke_tool(tool, {"url": "https://docs.example.com/long"}, make_ctx())
    # assert
    assert result.ok is True and result.output["truncated"] is True
    assert result.output["original_chars"] == BODY_PREVIEW_CHARS + 500
    assert len(result.output["text"]) <= BODY_PREVIEW_CHARS + 64  # 回灌有界
    assert result.output["spill"] == "in_memory" and "未注入" in result.output["spill_note"]


async def test_正文超20KB_注入spill存储_指针落盘原文可取回():
    # arrange：SpillStore 注入（组合根给工作区 tmp/未来 MinIO 同构实现）
    body = "<p>" + "密" * (BODY_PREVIEW_CHARS + 500) + "</p>"
    store = FakeSpillStore()
    tool, _ = build_web_bindings(
        ("docs.example.com",), client_factory=spy_factory(_html_transport(body)), spill_store=store
    )
    # act
    result = await invoke_tool(tool, {"url": "https://docs.example.com/long"}, make_ctx())
    # assert：output 只进预览，完整原文经 locator 可取（读侧契约）
    assert result.ok is True and result.output["spill"] == "stored"
    (key,) = store.payloads
    assert key.startswith("web/") and result.output["spill_locator"] == f"mem://{key}"
    stored = store.payloads[key]
    assert len(stored) == BODY_PREVIEW_CHARS + 500 and stored.endswith("密" * 10)


async def test_spill落盘失败_failopen_仍必须有界并留痕():
    # arrange
    body = "<p>" + "败" * (BODY_PREVIEW_CHARS + 500) + "</p>"
    tool, _ = build_web_bindings(
        ("docs.example.com",), client_factory=spy_factory(_html_transport(body)), spill_store=FakeSpillStore(fail=True)
    )
    # act
    result = await invoke_tool(tool, {"url": "https://docs.example.com/long"}, make_ctx())
    # assert：存储故障不反噬（fail-open），有界预览仍回灌 + persist_failed 留痕
    assert result.ok is True and result.output["spill"] == "persist_failed"
    assert "RuntimeError" in result.output["spill_note"]
    assert len(result.output["text"]) <= BODY_PREVIEW_CHARS + 64


async def test_上游超时_结构化失败5001():
    # arrange：MockTransport 直接抛 httpx 超时族（映射「超时=5001」同 chat 适配器先例）
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("连接超时", request=req)

    tool, _ = build_web_bindings(("docs.example.com",), client_factory=spy_factory(SpyTransport(handler)))
    # act
    result = await invoke_tool(tool, {"url": "https://docs.example.com/slow"}, make_ctx())
    # assert
    assert result.ok is False and result.error_code == int(ErrorCode.LLM_TIMEOUT)


async def test_超时必设_默认15秒_内核timeout_ms硬钳取小():
    # arrange：工厂桩记录每次注入的超时秒
    timeouts: list[float] = []
    tool, _ = build_web_bindings(
        ("docs.example.com",), client_factory=spy_factory(_html_transport("ok"), timeouts=timeouts)
    )
    ctx = make_ctx()
    # act
    await invoke_tool(tool, {"url": "https://docs.example.com/a"}, ctx)
    await invoke_tool(tool, {"url": "https://docs.example.com/b"}, ctx, timeout_ms=500)
    # assert：缺省 15s；内核 timeout_ms=500 → 硬钳 0.5s（取小）
    assert tool.timeout_s == DEFAULT_TIMEOUT_S == 15.0
    assert timeouts == [15.0, 0.5]


async def test_非文本媒体类型_结构化拒绝3004():
    # arrange
    transport = _html_transport(b"\x00\x01binary", content_type="application/octet-stream")
    tool, _ = build_web_bindings(("docs.example.com",), client_factory=spy_factory(transport))
    # act
    result = await invoke_tool(tool, {"url": "https://docs.example.com/blob"}, make_ctx())
    # assert
    assert result.ok is False and result.error_code == int(ErrorCode.UNSUPPORTED_MEDIA_TYPE)


async def test_上游404_结构化失败5003():
    # arrange
    tool, _ = build_web_bindings(
        ("docs.example.com",),
        client_factory=spy_factory(SpyTransport(lambda req: httpx.Response(404, text="not found"))),
    )
    # act
    result = await invoke_tool(tool, {"url": "https://docs.example.com/missing"}, make_ctx())
    # assert
    assert result.ok is False and result.error_code == int(ErrorCode.MCP_TARGET_UNAVAILABLE)
    assert "404" in (result.error_message or "")


# ── HTML 抽取（标准库 html.parser）───────────────────────────────────────


def test_HTML抽取_剔除script样式_保留title与文本结构():
    # arrange
    html = """
    <html><head><title>电网报告</title><style>p { color: red }</style></head>
    <body>
      <script>alert("注入探测")</script>
      <h1>结论</h1><p>线路过载导致跳闸</p>
      <div>附表<span>数字</span>收尾</div>
    </body></html>
    """
    # act
    text, title = extract_html_text(html)
    # assert：script/style 全剔除；title 捕获；块级换行结构保留
    assert title == "电网报告"
    assert "alert" not in text and "color" not in text
    assert "结论" in text and "线路过载导致跳闸" in text and "数字" in text
    assert "\n" in text and "  " not in text and "\n\n" not in text  # 空白折叠


# ── egress 审计 ──────────────────────────────────────────────────────────


async def test_fetch审计行_记录域名字节耗时_不含响应正文():
    # arrange
    body = "<p>机密正文标记XYZQ</p>"
    audit_sink, records = recording_audit()
    tool, _ = build_web_bindings(
        ("docs.example.com",), client_factory=spy_factory(_html_transport(body)), audit_sink=audit_sink
    )
    ctx = make_ctx()
    # act
    result = await invoke_tool(tool, {"url": "https://docs.example.com/audit"}, ctx)
    # assert：ok 行=域名/字节量/耗时/归属；正文永不入审计（红线）
    assert result.ok is True and len(records) == 1
    record = records[0]
    assert record["event"] == "web.egress" and record["tool"] == "web.fetch"
    assert record["host"] == "docs.example.com" and record["outcome"] == "ok"
    assert record["bytes"] == len(body.encode("utf-8")) and record["duration_ms"] >= 0
    assert record["trace_id"] == ctx.trace_id and record["call_id"]
    assert "机密正文标记XYZQ" not in json.dumps(records, ensure_ascii=False)


async def test_拒绝与失败同样落审计行():
    # arrange：一次白名单拒绝 + 一次上游失败
    audit_sink, records = recording_audit()
    transport = SpyTransport(lambda req: httpx.Response(500, text="boom"))
    tool, _ = build_web_bindings(("docs.example.com",), client_factory=spy_factory(transport), audit_sink=audit_sink)
    ctx = make_ctx()
    # act
    await invoke_tool(tool, {"url": "https://evil.example.org/x"}, ctx)
    await invoke_tool(tool, {"url": "https://docs.example.com/broken"}, ctx)
    # assert：denied 行不触达外呼也留痕；error 行带状态码——安全运营面
    outcomes = [(r["outcome"], r["host"]) for r in records]
    assert ("denied", "evil.example.org") in outcomes and ("error", "docs.example.com") in outcomes
    assert records[1]["status_code"] == 500


# ── web_search ───────────────────────────────────────────────────────────


async def test_search缺query参数_结构化拒绝3001():
    # arrange
    tool = WebSearchTool(EgressAllowlist(("docs.example.com",)), FakeSearch())
    # act
    result = await invoke_tool(tool, {}, make_ctx())
    # assert
    assert result.ok is False and result.error_code == int(ErrorCode.PARAM_INVALID)


async def test_search_Fake后端_结果按白名单过滤():
    # arrange：3 条结果中 1 条非白名单域（条目过滤同 fetch；子域命中）
    backend = FakeSearch(
        [
            SearchHit(title="命中A", url="https://docs.example.com/a", snippet="摘要A"),
            SearchHit(title="外域", url="https://evil.example.org/b", snippet="摘要B"),
            SearchHit(title="命中C", url="https://api.docs.example.com/c", snippet="摘要C"),
        ]
    )
    audit_sink, records = recording_audit()
    tool = WebSearchTool(EgressAllowlist(("docs.example.com",)), backend, audit_sink=audit_sink, default_max_results=5)
    # act
    result = await invoke_tool(tool, {"query": "停电分析"}, make_ctx())
    # assert
    assert result.ok is True
    assert [r["url"] for r in result.output["results"]] == [
        "https://docs.example.com/a",
        "https://api.docs.example.com/c",
    ]
    assert result.output["total"] == 3 and result.output["returned"] == 2
    assert records[0]["kept_results"] == 2
    assert "摘要" not in json.dumps(records, ensure_ascii=False)  # 摘要正文不入审计（红线）


async def test_search_端点不在白名单_拒绝2001零外呼():
    # arrange：DDG 后端端点未入白名单（fail-closed 同构：缺省空白名单同样全拒）
    transport = SpyTransport(lambda req: httpx.Response(200, text=DDG_HTML))
    backend = DuckDuckGoHtmlSearch(spy_factory(transport))
    tool = WebSearchTool(EgressAllowlist(("docs.example.com",)), backend)
    # act
    result = await invoke_tool(tool, {"query": "停电"}, make_ctx())
    # assert
    assert result.ok is False and result.error_code == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert transport.requests_by_host == {}  # 端点判定在前，零外呼


async def test_search_DDG适配_MockTransport解析uddg还原与摘要():
    # arrange：DDG html 端点 MockTransport（零真连）；搜索端点自身与结果域同入白名单方可外呼
    transport = SpyTransport(lambda req: httpx.Response(200, text=DDG_HTML, headers={"content-type": "text/html"}))
    backend = DuckDuckGoHtmlSearch(spy_factory(transport))
    tool = WebSearchTool(
        EgressAllowlist(("docs.example.com", "blog.other.org", "html.duckduckgo.com")), backend, default_max_results=10
    )
    # act
    result = await invoke_tool(tool, {"query": "停电分析"}, make_ctx())
    # assert：uddg 包裹链接还原 + 直链保留 + 摘要抽取
    assert result.ok is True and result.output["backend"] == "duckduckgo_html"
    hits = {hit["title"]: hit for hit in result.output["results"]}
    assert hits["停电分析指南"]["url"] == "https://docs.example.com/guide"
    assert hits["停电分析指南"]["snippet"] == "电网停电原因分析实操指南"
    assert hits["直链博文"]["url"] == "https://blog.other.org/post"


async def test_search_后端异常_结构化失败5003():
    # arrange：纯内存后端（endpoint_host=None 跳过端点判定），后端抛异常
    backend = FakeSearch(error=RuntimeError("引擎故障"))
    tool = WebSearchTool(EgressAllowlist(()), backend)
    # act
    result = await invoke_tool(tool, {"query": "停电"}, make_ctx())
    # assert
    assert result.ok is False and result.error_code == int(ErrorCode.MCP_TARGET_UNAVAILABLE)
    assert "引擎故障" in (result.error_message or "")


async def test_search_条目数钳制在硬上限():
    # arrange：缺省请求 999 条（>硬上限 10）
    backend = FakeSearch([SearchHit(title=f"t{i}", url=f"https://docs.example.com/{i}") for i in range(20)])
    tool = WebSearchTool(EgressAllowlist(("docs.example.com",)), backend, default_max_results=999)
    # act
    result = await invoke_tool(tool, {"query": "停电"}, make_ctx())
    # assert：MAX_RESULTS_CAP 硬钳（token 爆量入口防线）
    assert result.ok is True and result.output["returned"] == 10


# ── ToolPort 绑定形状与协议实现 ──────────────────────────────────────────


def test_绑定工厂_双工具满足ToolPort并注册进分发器():
    # arrange
    fetch_tool, search_tool = build_web_bindings(
        ("docs.example.com",), client_factory=spy_factory(_html_transport("x"))
    )
    dispatcher = ExtensionDispatcher()
    # act
    dispatcher.register_tool(fetch_tool)
    dispatcher.register_tool(search_tool)
    # assert：行动类 IRI 各就位；runtime_checkable Protocol 形状成立
    assert dispatcher.tool_for(WEB_FETCH_ACTION_IRI) is fetch_tool
    assert dispatcher.tool_for(WEB_SEARCH_ACTION_IRI) is search_tool
    assert isinstance(fetch_tool, ToolPort) and isinstance(search_tool, ToolPort)


async def test_spill存储协议可注入_本地目录实现落盘可取回(tmp_path: Path):
    # arrange：内核 spill 协议实现（LocalDirSpillStore，组合根可指到工作区 tmp）可注入本能力
    from services.agent.data.spill_store import LocalDirSpillStore

    store: SpillStore = LocalDirSpillStore(tmp_path)
    # act
    locator = await store.put("web/tenant/call.txt", "原文")
    # assert
    assert Path(locator).read_text(encoding="utf-8") == "原文"
