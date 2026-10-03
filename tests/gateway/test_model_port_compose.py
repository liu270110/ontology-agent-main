# tests/gateway/test_model_port_compose.py
"""组合根 _build_model_port 装配矛盾修复（docs/Agent/09 §2.1 工程问题清单）：

本地渠道（vLLM 等 OpenAI 兼容服务端）无密钥——.env 口径「OA_LLM_API_KEY 留空即可」，
但旧实现要求 base_url 与 api_key 同时非空才装配，本地渠道永远装配不出 ModelPort。
修复后：仅 llm_base_url 缺失才返回 None；key 留空传占位符 "EMPTY" 构造
（OpenAICompatibleModelPort 构造期拒绝 None/空串，服务端接受任意非空 Bearer）。
"""

from __future__ import annotations

from services.gateway.app import _build_model_port
from services.platform.config import Settings
from services.platform.llm.audited import AuditedModelPort


def test_仅配base_url_key留空_装配出可用端口而非None():
    # Arrange：本地 vLLM 渠道（key 留空）；显式 None 压过 OA_ 环境变量/.env，防本机配置串扰
    s = Settings(llm_base_url="http://127.0.0.1:8001/v1", llm_api_key=None)
    # Act
    port = _build_model_port(s)
    # Assert：返回审计包裹端口（占位符使内部客户端构造期校验通过，None/空串会抛 5002
    # ModelGatewayConfigError）；组合根装配面无公开读取口，装配契约以内部结构断言钉住
    assert isinstance(port, AuditedModelPort)
    inner = port._inner
    assert inner._base_url == "http://127.0.0.1:8001/v1"
    assert inner._client.headers["Authorization"] == "Bearer EMPTY"


def test_base_url与key全缺_返回None不装配():
    # Arrange：无任何 llm 配置（显式 None 压过 OA_ 环境变量/.env，防本机配置串扰）
    s = Settings(llm_base_url=None, llm_api_key=None)
    # Act
    port = _build_model_port(s)
    # Assert：不装配——不阻塞启动，extract 步以 5002 LLM_UNAVAILABLE 失败（可重跑口径不变）
    assert port is None
