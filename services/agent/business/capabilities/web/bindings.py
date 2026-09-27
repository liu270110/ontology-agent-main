"""web 能力绑定工厂（docs/Agent/06 #3：经 ToolPort 绑定进内核，B1 门禁链照常生效）。

组合根（gateway/mcp bootstrap 等）接线片段：

    from services.agent.business.capabilities.web import build_web_bindings

    fetch_tool, search_tool = build_web_bindings(
        fetch_allowlist=("docs.example.com", "api.example.com"),
        search_backend=None,  # 缺省 DuckDuckGo HTML 轻适配；生产注入 SerpAPI/Brave 同协议实现
        spill_store=LocalDirSpillStore(settings.task_spill_dir),  # 可选：正文落盘换指针
    )
    dispatcher.register_tool(fetch_tool)
    dispatcher.register_tool(search_tool)

缺省语义（fail-closed）：``fetch_allowlist=()`` 时 fetch 全拒、search 条目全滤——
出口白名单是安全边界而非功能开关，部署方按租户显式下发域名列。
"""

from __future__ import annotations

from collections.abc import Iterable

from services.agent.business.capabilities.web.allowlist import EgressAllowlist
from services.agent.business.capabilities.web.audit import AuditSink
from services.agent.business.capabilities.web.fetch import (
    DEFAULT_TIMEOUT_S,
    HttpClientFactory,
    WebFetchTool,
)
from services.agent.business.capabilities.web.search import (
    DuckDuckGoHtmlSearch,
    SearchBackend,
    WebSearchTool,
)
from services.agent.business.kernel.spill import SpillStore


def build_web_bindings(
    fetch_allowlist: EgressAllowlist | Iterable[str],
    search_backend: SearchBackend | None = None,
    *,
    client_factory: HttpClientFactory | None = None,
    spill_store: SpillStore | None = None,
    audit_sink: AuditSink | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[WebFetchTool, WebSearchTool]:
    """装配 web_fetch + web_search 双工具绑定（一能力一目录，双工具同源白名单）。

    - ``fetch_allowlist``：域名白名单（EgressAllowlist 或域名可迭代；空白名单=全拒）；
    - ``search_backend``：SearchBackend 实现（None=缺省 DuckDuckGo HTML 轻适配）；
    - ``client_factory``：HTTP 客户端工厂（tests 注入 MockTransport；缺省真连）；
    - ``spill_store``：正文溢出存储（缺省 None=内存截断并注明）；
    - ``audit_sink``：egress 审计汇（缺省标准日志行）；
    - ``timeout_s``：外呼超时缺省 15s（实际生效与内核 timeout_ms 取小硬钳）。
    """
    allowlist = fetch_allowlist if isinstance(fetch_allowlist, EgressAllowlist) else EgressAllowlist(fetch_allowlist)
    backend = search_backend if search_backend is not None else DuckDuckGoHtmlSearch(client_factory)
    fetch_tool = WebFetchTool(
        allowlist,
        client_factory=client_factory,
        spill_store=spill_store,
        audit_sink=audit_sink,
        timeout_s=timeout_s,
    )
    search_tool = WebSearchTool(allowlist, backend, audit_sink=audit_sink, timeout_s=timeout_s)
    return fetch_tool, search_tool
