"""web 能力（P0 #3：web_fetch/web_search；docs/Agent/06 #3 路线 + 研究整理/08 C3 依据）。

面（B4 白名单出口 / B3 agent_attested 标界 / 截断+spill 指针 / egress 审计）见各模块
docstring；组合根经 :func:`build_web_bindings` 装配后 register_tool 进内核分发器。
"""

from __future__ import annotations

from services.agent.business.capabilities.web.allowlist import ALLOWED_SCHEMES, EgressAllowlist, hostname_of
from services.agent.business.capabilities.web.audit import AuditSink, default_audit_sink, emit_audit
from services.agent.business.capabilities.web.bindings import build_web_bindings
from services.agent.business.capabilities.web.extract import extract_html_text
from services.agent.business.capabilities.web.fetch import (
    BODY_PREVIEW_CHARS,
    DEFAULT_TIMEOUT_S,
    MAX_RESPONSE_BYTES,
    WEB_FETCH_ACTION_IRI,
    WebFetchTool,
)
from services.agent.business.capabilities.web.search import (
    MAX_RESULTS_CAP,
    WEB_SEARCH_ACTION_IRI,
    DuckDuckGoHtmlSearch,
    SearchBackend,
    SearchHit,
    WebSearchTool,
)

__all__ = [
    "ALLOWED_SCHEMES",
    "BODY_PREVIEW_CHARS",
    "DEFAULT_TIMEOUT_S",
    "DuckDuckGoHtmlSearch",
    "EgressAllowlist",
    "MAX_RESPONSE_BYTES",
    "MAX_RESULTS_CAP",
    "AuditSink",
    "SearchBackend",
    "SearchHit",
    "WEB_FETCH_ACTION_IRI",
    "WEB_SEARCH_ACTION_IRI",
    "WebFetchTool",
    "WebSearchTool",
    "build_web_bindings",
    "default_audit_sink",
    "emit_audit",
    "extract_html_text",
    "hostname_of",
]
