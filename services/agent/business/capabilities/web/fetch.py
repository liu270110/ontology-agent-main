"""web_fetch 工具（tools.bindings，L0；docs/Agent/06 #3 路线 + 研究整理/08 C3）。

安全面（全部硬约束，缺失即拒绝 fail-closed）：

- **白名单出口（B4）**：每跳请求前域名复检，缺省空白名单=全拒；重定向逐跳复检，
  跨白名单跳转在请求发出前拦截（不触达目标主机）。
- **超时必设**：缺省 15s，与内核 ``timeout_ms`` 建议上限取小（硬钳，内核 A4 同口径）。
- **响应大小上限**：2MB 流式累计，超限即结构化失败（防内存打爆）。
- **正文截断 + spill 指针**：抽取正文 >20KB 时仅回预览；注入 SpillStore 则原文落盘
  （复用内核 spill 协议，组合根给 LocalDirSpillStore/未来 MinIO 同构实现），落盘失败
  fail-open 仍必须有界（内核 spill 同款）；未注入则内存截断并在 output 注明（原文不持久化）。
- **B3 标界**：正文=不可信外部输入，不自称 externally_verified；output 携带
  来源 URL/抓取时间/截断标记等 agent_attested 元数据。
- **egress 审计**：每次外呼（含重定向/拒绝/失败）落结构化日志（域名/字节/耗时），禁记正文。

错误码映射（全部 02 §7 已登记码，禁新增）：参数/协议非法=3001；白名单拒绝=2001；
超时=5001（同 chat 适配器「超时=5001」先例）；上游不可用/状态错误/超限=5003；
媒体类型不支持=3004；其余兜底=5999。失败一律结构化 ToolResult，禁裸异常逃逸。
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from services.agent.business.capabilities.web.allowlist import EgressAllowlist, hostname_of
from services.agent.business.capabilities.web.audit import AuditSink, default_audit_sink, emit_audit
from services.agent.business.capabilities.web.extract import extract_html_text
from services.agent.business.kernel.spill import SPILL_MARKER_FMT, SpillStore
from services.agent.domain.model.kernel_actions import ApprovalTicket, ToolCall, ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.errors import ErrorCode

WEB_FETCH_ACTION_IRI = "http://ontology.example/action/web_fetch"

DEFAULT_TIMEOUT_S = 15.0  # 超时必设（缺省 15s；实际生效与内核 timeout_ms 取小）
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 响应大小上限 2MB（流式累计硬顶）
BODY_PREVIEW_CHARS = 20_000  # 回灌正文预览上限（超出 spill/注明）
MAX_REDIRECT_HOPS = 5  # 重定向跳数上限（防跳转环）
# K14-c（docs/Agent/13 §20）：截断标记收敛同源——与内核 spill 预览同一 SPILL_MARKER_FMT
# 格式；locator 已知时内嵌指针（模型在预览内即可见兑换入口），未知（未注入 store/落盘
# 失败）时给无指针兜底标注。
_SPILL_MARK_FALLBACK = "\n…" + SPILL_MARKER_FMT.format(locator="不可用：原文未持久化，仅本次内存截断") + "…"


def _truncation_mark(locator: str | None) -> str:
    """统一截断标注（K14-c 同源）：有 locator 内嵌指针，无则兜底（读侧经 spill.get 兑换）。"""
    if locator is None:
        return _SPILL_MARK_FALLBACK
    return "\n…" + SPILL_MARKER_FMT.format(locator=locator) + "…"


# HTTP 客户端工厂（组合根/tests 注入 MockTransport；入参=本次调用的钳制后超时秒）
HttpClientFactory = Callable[[float], httpx.AsyncClient]

_CHARSET_RE = re.compile(r"charset=([\w\-]+)", re.IGNORECASE)

# 文本类媒体类型（html/xhtml/xml 走抽取，其余文本按原样；之外一律 3004）
_HTML_MEDIA = ("text/html", "application/xhtml+xml")


def default_client_factory(timeout_s: float) -> httpx.AsyncClient:
    """缺省客户端：真连出口（超时必设 + 手动跟随重定向，便于逐跳白名单复检）。"""
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), follow_redirects=False)


def _charset_from_content_type(content_type: str) -> str:
    match = _CHARSET_RE.search(content_type)
    return match.group(1) if match else "utf-8"


def _decode(raw: bytes, charset: str) -> str:
    """按声明字符集解码；未知/坏字符集降级 utf-8 replace（不可信输入禁异常逃逸）。"""
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeError):
        return raw.decode("utf-8", errors="replace")


class _ResponseTooLargeError(Exception):
    """响应超限内部信号（转 5003 结构化失败，不逃逸）。"""

    def __init__(self, size: int) -> None:
        super().__init__(f"响应体超过 {MAX_RESPONSE_BYTES} 字节上限（当前 {size}）")


class WebFetchTool:
    """URL 抓取工具（ToolPort）：白名单出口 + 截断 + spill + B3 标界元数据 + egress 审计。"""

    meta = ExtensionMeta(
        name="web.fetch",
        version="1.0.0",
        semantic_annotation={"action_iri": WEB_FETCH_ACTION_IRI},
    )

    def __init__(
        self,
        allowlist: EgressAllowlist,
        *,
        client_factory: HttpClientFactory | None = None,
        spill_store: SpillStore | None = None,
        audit_sink: AuditSink | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._allowlist = allowlist
        self._client_factory: HttpClientFactory = client_factory or default_client_factory
        self._spill_store = spill_store
        self._audit_sink: AuditSink = audit_sink or default_audit_sink
        self._timeout_s = timeout_s

    @property
    def timeout_s(self) -> float:
        """配置超时（缺省 15s；实际生效取与内核 timeout_ms 的较小值）。"""
        return self._timeout_s

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        del approval  # READ 行动无审批面（B5 路由不做、实现不自查授权，B1 R3）
        url = str(call.parameters.get("url") or "").strip()
        if not url:
            return self._fail(int(ErrorCode.PARAM_INVALID), "缺少 url 参数")
        try:
            parts = urlsplit(url)
        except ValueError:
            return self._fail(int(ErrorCode.PARAM_INVALID), f"url 非法: {url[:200]}")
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return self._fail(int(ErrorCode.PARAM_INVALID), f"url 仅支持 http/https 且需含主机: {url[:200]}")

        timeout_s = min(self._timeout_s, max(timeout_ms, 1) / 1000)  # 内核建议上限硬钳（取小）
        started = time.monotonic()
        if not self._allowlist.allows_url(url):  # 首跳白名单判定在客户端装配之前（fail-closed 零资源开销）
            self._audit(ctx, call, host=hostname_of(url), outcome="denied", started=started)
            return self._fail(int(ErrorCode.SCOPE_INSUFFICIENT), f"出口白名单拒绝: {hostname_of(url) or url[:200]}")
        try:
            client = self._client_factory(timeout_s)
        except Exception as exc:  # noqa: BLE001 —— 装配失败结构化返回（禁裸异常逃逸）
            return self._fail(int(ErrorCode.INTERNAL_ERROR), f"HTTP 客户端装配失败: {exc}")
        try:
            return await self._fetch_with_redirects(url, client, call, ctx, started)
        finally:
            with contextlib.suppress(Exception):  # 关闭失败不反噬结果
                await client.aclose()

    # ── 抓取主流程：逐跳白名单复检 → 流式限长读取 ─────────────────────────
    async def _fetch_with_redirects(
        self,
        url: str,
        client: httpx.AsyncClient,
        call: ToolCall,
        ctx: TenantContext,
        started: float,
    ) -> ToolResult:
        current = url
        for hop in range(1, MAX_REDIRECT_HOPS + 1):
            host = hostname_of(current)
            if not self._allowlist.allows_url(current):
                self._audit(ctx, call, host=host, outcome="denied", started=started, redirect_hops=hop - 1)
                return self._fail(int(ErrorCode.SCOPE_INSUFFICIENT), f"出口白名单拒绝: {host or current[:200]}")
            content_type = ""
            raw = b""
            status = 0
            try:
                async with client.stream("GET", current) as resp:
                    status = resp.status_code
                    content_type = resp.headers.get("content-type", "")
                    if 300 <= status < 400:  # 重定向：下一跳请求发出前复检（跨白名单不触达）
                        location = resp.headers.get("location", "")
                        if not location:
                            self._audit(
                                ctx,
                                call,
                                host=host,
                                outcome="error",
                                started=started,
                                status_code=status,
                                redirect_hops=hop,
                            )
                            return self._fail(int(ErrorCode.MCP_TARGET_UNAVAILABLE), f"重定向缺 Location（第{hop}跳）")
                        current = urljoin(current, location)
                        self._audit(
                            ctx,
                            call,
                            host=host,
                            outcome="redirect",
                            started=started,
                            status_code=status,
                            redirect_hops=hop,
                        )
                        continue
                    if status >= 400:
                        self._audit(
                            ctx,
                            call,
                            host=host,
                            outcome="error",
                            started=started,
                            status_code=status,
                            redirect_hops=hop,
                        )
                        return self._fail(int(ErrorCode.MCP_TARGET_UNAVAILABLE), f"上游状态码 {status}: {host}")
                    raw = await self._read_bounded(resp)
            except httpx.TimeoutException as exc:
                self._audit(ctx, call, host=host, outcome="timeout", started=started, redirect_hops=hop)
                return self._fail(int(ErrorCode.LLM_TIMEOUT), f"抓取超时: {type(exc).__name__}")
            except _ResponseTooLargeError as exc:
                self._audit(
                    ctx, call, host=host, outcome="error", started=started, status_code=status, redirect_hops=hop
                )
                return self._fail(int(ErrorCode.MCP_TARGET_UNAVAILABLE), str(exc))
            except httpx.HTTPError as exc:
                self._audit(ctx, call, host=host, outcome="error", started=started, redirect_hops=hop)
                return self._fail(
                    int(ErrorCode.MCP_TARGET_UNAVAILABLE), f"上游不可用: {type(exc).__name__}: {exc}"[:300]
                )
            except Exception as exc:  # noqa: BLE001 —— 其余异常兜底结构化（禁裸异常逃逸）
                self._audit(ctx, call, host=host, outcome="error", started=started, redirect_hops=hop)
                return self._fail(int(ErrorCode.INTERNAL_ERROR), f"抓取失败: {type(exc).__name__}: {exc}"[:300])
            self._audit(
                ctx,
                call,
                host=host,
                outcome="ok",
                started=started,
                status_code=status,
                bytes_read=len(raw),
                redirect_hops=hop,
            )
            return await self._build_result(current, url, raw, content_type, status, hop, call, ctx)
        return self._fail(int(ErrorCode.MCP_TARGET_UNAVAILABLE), f"重定向跳数超限（>{MAX_REDIRECT_HOPS}）")

    async def _read_bounded(self, resp: httpx.Response) -> bytes:
        """流式累计读取，超 2MB 即失败（防内存打爆；MockTransport/真连同语义）。"""
        buffer = bytearray()
        async for chunk in resp.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer) > MAX_RESPONSE_BYTES:
                raise _ResponseTooLargeError(len(buffer))
        return bytes(buffer)

    # ── 正文加工：媒体类型门 → HTML 抽取 → 截断 + spill ───────────────────
    async def _build_result(
        self,
        final_url: str,
        requested_url: str,
        raw: bytes,
        content_type: str,
        status: int,
        hops: int,
        call: ToolCall,
        ctx: TenantContext,
    ) -> ToolResult:
        media = content_type.split(";")[0].strip().lower()
        if media in _HTML_MEDIA or media.endswith(("+xml", "/xml")):
            text, title = extract_html_text(_decode(raw, _charset_from_content_type(content_type)))
        elif media.startswith("text/") or "json" in media:
            text, title = _decode(raw, _charset_from_content_type(content_type)), ""
        else:
            return self._fail(int(ErrorCode.UNSUPPORTED_MEDIA_TYPE), f"不支持的媒体类型: {media or '(未声明)'}")

        truncated = len(text) > BODY_PREVIEW_CHARS
        # K14-c：先落盘后拼预览——截断标注可内嵌真实 locator（同源格式，模型在预览内
        # 即可见兑换指针；spill 元数据键 spill/spill_note/spill_locator 保持不变）。
        spill_meta: dict[str, Any] = await self._spill(text, ctx, call) if truncated else {}
        output: dict[str, Any] = {
            "requested_url": requested_url,  # B3 agent_attested 元数据：来源与抓取事实
            "url": final_url,
            "fetched_at": datetime.now(UTC).isoformat(),
            "status_code": status,
            "content_type": content_type[:100],
            "title": title,
            "redirect_hops": hops,
            "original_bytes": len(raw),
            "original_chars": len(text),
            "truncated": truncated,
            "text": (
                text[:BODY_PREVIEW_CHARS] + (_truncation_mark(spill_meta.get("spill_locator")) if truncated else "")
            ),
            **spill_meta,
        }
        return ToolResult(ok=True, output=output)  # B3：不自称 externally_verified，信任级由内核标界

    async def _spill(self, text: str, ctx: TenantContext, call: ToolCall) -> dict[str, Any]:
        """原文落盘换指针（SpillStore 可注入）；未注入=内存截断并注明；落盘失败 fail-open 仍有界。"""
        if self._spill_store is None:
            return {
                "spill": "in_memory",
                "spill_note": "未注入 spill 存储：仅内存截断，原文未持久化（组合根注入 SpillStore 后落盘换指针）",
            }
        key = f"web/{ctx.tenant_id}/{call.call_id}.txt"
        try:
            locator = await self._spill_store.put(key, text)
        except Exception as exc:  # noqa: BLE001 —— 存储故障 fail-open：有界预览仍回灌（内核 spill 同款）
            return {"spill": "persist_failed", "spill_note": f"落盘失败: {type(exc).__name__}: {exc}"[:200]}
        return {"spill": "stored", "spill_locator": locator}

    # ── 审计与失败构造 ────────────────────────────────────────────────────
    def _audit(
        self,
        ctx: TenantContext,
        call: ToolCall,
        *,
        host: str,
        outcome: str,
        started: float,
        status_code: int | None = None,
        bytes_read: int | None = None,
        redirect_hops: int | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "event": "web.egress",
            "tool": self.meta.name,
            "host": host,
            "outcome": outcome,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "status_code": status_code,
            "bytes": bytes_read,
            "redirect_hops": redirect_hops,
            "trace_id": ctx.trace_id,
            "tenant_id": str(ctx.tenant_id),
            "call_id": str(call.call_id),
        }
        emit_audit(self._audit_sink, record)  # 禁记录响应正文（红线）

    @staticmethod
    def _fail(error_code: int, message: str) -> ToolResult:
        return ToolResult(ok=False, error_code=error_code, error_message=message[:300])
