"""web_search 工具（tools.bindings，L0；docs/Agent/06 #3 路线 + 研究整理/08 C3）。

形状：**SearchBackend Protocol + 可注入实现**（05 篇 ADR-6 同构：适配器可换、协议不换）。
本批交付 DuckDuckGo HTML 端点轻适配（无 key/无新依赖，纯标准库解析）作为缺省实现；
取舍论证：

- DDG html 端点为非官方 HTML 面，随站点改版可能失效——**仅作开发/演练缺省**，
  生产换 SerpAPI/Brave/内部检索适配（实现同协议注入，工具侧零改动）；
- 禁引入搜索 API 新依赖/key（路线硬约束），故不做官方 API 适配。

白名单同 fetch：后端自身端点先过 B4 判定（不在白名单=拒绝且零外呼）；**结果条目逐条
按同一白名单过滤**（只回可直接被 web_fetch 消费的域名）。缺省空白名单=端点拒绝+条目
全滤（fail-closed 同构；Fake 内存后端 ``endpoint_host=None`` 可跳过端点判定做纯逻辑测试）。
egress 审计与 fetch 同面（域名/耗时/条目数），禁落 query 原文与结果摘要正文（红线）。
"""

from __future__ import annotations

import asyncio
import time
from html.parser import HTMLParser
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from services.agent.business.capabilities.web.allowlist import EgressAllowlist
from services.agent.business.capabilities.web.audit import AuditSink, default_audit_sink, emit_audit
from services.agent.business.capabilities.web.fetch import DEFAULT_TIMEOUT_S, HttpClientFactory
from services.agent.domain.model.kernel_actions import ApprovalTicket, ToolCall, ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.errors import ErrorCode

WEB_SEARCH_ACTION_IRI = "http://ontology.example/action/web_search"

DEFAULT_MAX_RESULTS = 5  # 缺省条目数
MAX_RESULTS_CAP = 10  # 条目数硬上限（防 token 爆量，内核 spill 之前的入口防线）
_DDG_ENDPOINT = "https://html.duckduckgo.com/html/"


class SearchHit(BaseModel):
    """搜索结果条目（值对象 frozen）：title/url/snippet，一律不可信外部输入（B3）。"""

    model_config = ConfigDict(frozen=True)

    title: str
    url: str
    snippet: str = ""


@runtime_checkable
class SearchBackend(Protocol):
    """搜索引擎适配面（可注入实现；调用方=WebSearchTool，白名单过滤在工具侧统一执行）。"""

    name: str
    endpoint_host: str | None  # 后端自身出呼端点；None=纯内存实现（无外呼，跳过端点判定）

    async def search(self, query: str, *, max_results: int, timeout_s: float) -> list[SearchHit]:
        """执行检索；失败抛异常由工具侧结构化转义（禁半成品条目混入）。"""
        ...  # pragma: no cover — Protocol 方法无实现


class DuckDuckGoHtmlSearch:
    """DuckDuckGo HTML 端点轻适配（``html.duckduckgo.com/html``）：无 key/无新依赖。

    client_factory 可注入（tests 用 MockTransport 零真连）；解析容错——改版导致的
    结构变化降级为空结果/少条目，不抛异常（正文/条目一律不可信，工具侧还会再过滤）。
    """

    name = "duckduckgo_html"
    endpoint_host = "html.duckduckgo.com"

    def __init__(self, client_factory: HttpClientFactory | None = None) -> None:
        self._client_factory = client_factory

    async def search(self, query: str, *, max_results: int, timeout_s: float) -> list[SearchHit]:
        if self._client_factory is None:
            client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), follow_redirects=True)
        else:
            client = self._client_factory(timeout_s)
        try:
            resp = await client.get(_DDG_ENDPOINT, params={"q": query})
            resp.raise_for_status()
            return _parse_ddg_results(resp.text)[:max_results]
        finally:
            await client.aclose()


class _DDGParser(HTMLParser):
    """DDG html 结果解析：``a.result__a``=标题+链接、``a.result__snippet``=摘要。

    状态机：标题锚开新条目（uddg 包裹链接还原），摘要锚补全并落账；锚间无嵌套 `<a>`。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: list[SearchHit] = []
        self._pending: SearchHit | None = None
        self._mode = ""  # "" | "title" | "snippet"
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        classes = (dict(attrs).get("class") or "").split()
        if "result__a" in classes:
            self._flush_pending()
            href = _unwrap_ddg_href(dict(attrs).get("href") or "")
            self._pending = SearchHit(title="", url=href)
            self._mode, self._buf = "title", []
        elif "result__snippet" in classes and self._pending is not None:
            self._mode, self._buf = "snippet", []

    def handle_data(self, data: str) -> None:
        if self._mode:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._mode:
            return
        text = " ".join("".join(self._buf).split())
        if self._mode == "title" and self._pending is not None:
            self._pending = self._pending.model_copy(update={"title": text})
        elif self._mode == "snippet" and self._pending is not None:
            self.hits.append(self._pending.model_copy(update={"snippet": text}))
            self._pending = None
        self._mode, self._buf = "", []

    def _flush_pending(self) -> None:
        """新标题锚到来仍未落账的条目（无摘要）：按现状落账，不丢条目。"""
        if self._pending is not None:
            self.hits.append(self._pending)
            self._pending = None


def _parse_ddg_results(html: str) -> list[SearchHit]:
    parser = _DDGParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 —— 改版/畸形页面降级为空结果（不可信输入禁异常逃逸）
        return []
    return parser.hits


def _unwrap_ddg_href(href: str) -> str:
    """DDG 包裹链接（``//duckduckgo.com/l/?uddg=<encoded>``）→ 目标 URL；直链原样返回。"""
    href = href.strip()
    if href.startswith("//"):
        href = "https:" + href
    try:
        parts = urlsplit(href)
        if (parts.hostname or "").endswith("duckduckgo.com") and "uddg" in parse_qs(parts.query):
            return parse_qs(parts.query)["uddg"][0]
    except ValueError:
        pass
    return href


class WebSearchTool:
    """搜索工具（ToolPort）：端点 B4 判定 → 后端检索 → 结果按白名单过滤 + egress 审计。"""

    meta = ExtensionMeta(
        name="web.search",
        version="1.0.0",
        semantic_annotation={"action_iri": WEB_SEARCH_ACTION_IRI},
    )

    def __init__(
        self,
        allowlist: EgressAllowlist,
        backend: SearchBackend,
        *,
        audit_sink: AuditSink | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        default_max_results: int = DEFAULT_MAX_RESULTS,
    ) -> None:
        self._allowlist = allowlist
        self._backend = backend
        self._audit_sink: AuditSink = audit_sink or default_audit_sink
        self._timeout_s = timeout_s
        self._default_max_results = default_max_results

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        del approval  # READ 行动无审批面（B5 路由不做、实现不自查授权，B1 R3）
        query = str(call.parameters.get("query") or "").strip()
        if not query:
            return self._fail(int(ErrorCode.PARAM_INVALID), "缺少 query 参数")
        try:
            requested = int(call.parameters.get("max_results") or self._default_max_results)
        except (TypeError, ValueError):
            return self._fail(int(ErrorCode.PARAM_INVALID), "max_results 须为整数")
        max_results = max(1, min(requested, MAX_RESULTS_CAP))

        endpoint = self._backend.endpoint_host
        timeout_s = min(self._timeout_s, max(timeout_ms, 1) / 1000)  # 内核建议上限硬钳（取小）
        started = time.monotonic()
        if endpoint is not None and not self._allowlist.allows_host(endpoint):
            self._audit(ctx, call, host=endpoint, outcome="denied", started=started)
            return self._fail(int(ErrorCode.SCOPE_INSUFFICIENT), f"搜索端点不在出口白名单: {endpoint}")
        try:
            hits = await asyncio.wait_for(
                self._backend.search(query, max_results=max_results, timeout_s=timeout_s), timeout=timeout_s
            )
        except TimeoutError:
            self._audit(ctx, call, host=endpoint or "none", outcome="timeout", started=started)
            return self._fail(int(ErrorCode.LLM_TIMEOUT), "搜索超时")
        except Exception as exc:  # noqa: BLE001 —— 后端失败结构化返回（禁裸异常逃逸）
            self._audit(ctx, call, host=endpoint or "none", outcome="error", started=started)
            return self._fail(int(ErrorCode.MCP_TARGET_UNAVAILABLE), f"搜索后端失败: {type(exc).__name__}: {exc}"[:300])
        kept = [hit for hit in hits if self._allowlist.allows_url(hit.url)]  # 白名单过滤同 fetch
        self._audit(
            ctx,
            call,
            host=endpoint or "none",
            outcome="ok",
            started=started,
            result_count=len(hits),
            kept_results=len(kept),
        )
        output: dict[str, Any] = {
            "query": query,
            "backend": self._backend.name,
            "total": len(hits),
            "returned": len(kept),
            "results": [hit.model_dump() for hit in kept],
        }
        return ToolResult(ok=True, output=output)  # B3：不自称 externally_verified，信任级由内核标界

    def _audit(
        self,
        ctx: TenantContext,
        call: ToolCall,
        *,
        host: str,
        outcome: str,
        started: float,
        result_count: int | None = None,
        kept_results: int | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "event": "web.egress",
            "tool": self.meta.name,
            "host": host,
            "outcome": outcome,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "trace_id": ctx.trace_id,
            "tenant_id": str(ctx.tenant_id),
            "call_id": str(call.call_id),
        }
        if result_count is not None:
            record["result_count"] = result_count
        if kept_results is not None:
            record["kept_results"] = kept_results
        emit_audit(self._audit_sink, record)  # 禁落 query 原文与结果摘要（红线）

    @staticmethod
    def _fail(error_code: int, message: str) -> ToolResult:
        return ToolResult(ok=False, error_code=error_code, error_message=message[:300])
