# tests/mcp/test_a2a_model_port_compose.py
"""A2A 独立入口 _build_model_port 装配矛盾修复（docs/Agent/09 §2.1 遗留项②）：

services/mcp/a2a/__main__.py 旧实现与 gateway 组合根修复前同款——要求 llm_base_url 与
llm_api_key 同时非空才装配，本地 vLLM 渠道（.env 口径「OA_LLM_API_KEY 留空即可」）委托
对话恒 5002 LLM_UNAVAILABLE。修复后与 gateway 对齐：仅 llm_base_url 缺失才返回 None；
key 留空传占位符 "EMPTY" 构造（OpenAICompatibleModelPort 构造期拒绝 None/空串，服务端
接受任意非空 Bearer）。
"""

from __future__ import annotations

from services.mcp.a2a.__main__ import _build_model_port
from services.platform.config import Settings
from services.platform.llm.audited import AuditedModelPort


def test_仅配base_url_key留空_装配出可用端口而非None():
    # Arrange：本地 vLLM 渠道（key 留空）；显式 None 压过 OA_ 环境变量/.env，防本机配置串扰
    s = Settings(llm_base_url="http://127.0.0.1:8001/v1", llm_api_key=None)
    # Act
    port = _build_model_port(s)
    # Assert：返回审计包裹端口（占位符使内部客户端构造期校验通过，None/空串会抛 5002
    # ModelGatewayConfigError）；独立入口装配面无公开读取口，装配契约以内部结构断言钉住
    assert isinstance(port, AuditedModelPort)
    inner = port._inner
    assert inner._base_url == "http://127.0.0.1:8001/v1"
    assert inner._client.headers["Authorization"] == "Bearer EMPTY"


def test_base_url与key全缺_返回None不装配():
    # Arrange：无任何 llm 配置（显式 None 压过 OA_ 环境变量/.env，防本机配置串扰）
    s = Settings(llm_base_url=None, llm_api_key=None)
    # Act
    port = _build_model_port(s)
    # Assert：不装配——builtin 不注册，委托对话以 5002 LLM_UNAVAILABLE 落 failed 终态
    assert port is None
