# tests/gateway/test_model_port_compose.py
"""组合根 _build_model_port 装配矛盾修复（docs/Agent/09 §2.1 工程问题清单）：

本地渠道（vLLM 等 OpenAI 兼容服务端）无密钥——.env 口径「OA_LLM_API_KEY 留空即可」，
但旧实现要求 base_url 与 api_key 同时非空才装配，本地渠道永远装配不出 ModelPort。
修复后：仅 llm_base_url 缺失才返回 None；key 留空传占位符 "EMPTY" 构造
（OpenAICompatibleModelPort 构造期拒绝 None/空串，服务端接受任意非空 Bearer）。

M4.5-C（docs/Agent/12 §3）：装配面升级为
FailoverModelPort( AuditedModelPort( OpenAICompatibleModelPort ) )——韧性层在审计层之外
（每次重试/降级尝试各过一次审计）；单凭证（extra 空串）不建凭证池=原直驱零行为变化。
"""

from __future__ import annotations

from services.gateway.app import _build_model_port
from services.platform.config import Settings
from services.platform.llm.audited import AuditedModelPort
from services.platform.llm.gateway import OpenAICompatibleModelPort
from services.platform.llm.resilience import FailoverModelPort


def test_仅配base_url_key留空_装配出可用端口而非None():
    # Arrange：本地 vLLM 渠道（key 留空）；显式 None 压过 OA_ 环境变量/.env，防本机配置串扰
    s = Settings(llm_base_url="http://127.0.0.1:8001/v1", llm_api_key=None)
    # Act
    port = _build_model_port(s)
    # Assert：韧性层在外、审计层居中、OpenAI 兼容客户端在内（占位符使内部客户端构造期
    # 校验通过，None/空串会抛 5002 ModelGatewayConfigError）；组合根装配面无公开读取口，
    # 装配契约以内部结构断言钉住
    assert isinstance(port, FailoverModelPort)
    audited = port._inner
    assert isinstance(audited, AuditedModelPort)
    channel = audited._inner
    assert isinstance(channel, OpenAICompatibleModelPort)
    assert channel._base_url == "http://127.0.0.1:8001/v1"
    assert channel._client.headers["Authorization"] == "Bearer EMPTY"
    assert channel._credential_pool is None  # 单凭证=不建池（零行为变化口径）
    assert port.audit is audited.audit  # lifespan flush 循环持有口透传


def test_base_url与key全缺_返回None不装配():
    # Arrange：无任何 llm 配置（显式 None 压过 OA_ 环境变量/.env，防本机配置串扰）
    s = Settings(llm_base_url=None, llm_api_key=None)
    # Act
    port = _build_model_port(s)
    # Assert：不装配——不阻塞启动，extract 步以 5002 LLM_UNAVAILABLE 失败（可重跑口径不变）
    assert port is None
