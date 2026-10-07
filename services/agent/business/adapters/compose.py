"""G2 通用适配器组合根装配面（docs/Agent/20 §3：Settings + profile 数据文件 → 适配器实例）。

- 全部可变参数走 Settings（agent_http_generic_profile 等六键，platform/config.py）；
  缺省空=零行为变化（不装配、不注册——编排器 adapters 字典无新键，现行行为不变）；
- profile 目录：Settings.agent_adapter_profiles_root 覆盖，空=内置 adapters/profiles/；
- 配置了不存在的 profile id / profile 装载失败 → 跳过 + 告警（fail-soft，skills catalog
  同款口径）；该 agent_tool 的对话请求编排器报「适配器未注册」（5002 结构化，可观测）；
- 会话映射：http-generic 经 PgAdapterSessionMapper（adapter_sessions 表）；cli-generic
  run-per-turn 无会话（05 篇 §4.4）不消费映射表。
"""

from __future__ import annotations

import logging
from typing import Any

from services.agent.business.adapters.base import ChatAdapter
from services.agent.business.adapters.cli_jsonl import CliJsonlAdapter
from services.agent.business.adapters.http_service import HttpServiceAdapter
from services.agent.business.adapters.profiles import DEFAULT_PROFILES_ROOT, scan_profiles
from services.agent.business.adapters.session_map import PgAdapterSessionMapper

logger = logging.getLogger(__name__)


def build_generic_adapters(settings: Any, *, session_factory: Any) -> dict[str, ChatAdapter]:
    """按 Settings 装配 G2 通用适配器（组合根 sessions.get_or_build_chat_orchestrator 消费）。

    返回键=适配器路由键（command.adapter 值）：``http-generic``/``cli-generic``；未配置对应
    Settings 键则不产出该键（零行为变化）。
    """
    root = getattr(settings, "agent_adapter_profiles_root", "") or DEFAULT_PROFILES_ROOT
    profiles = scan_profiles(root)
    adapters: dict[str, ChatAdapter] = {}

    profile_id = getattr(settings, "agent_http_generic_profile", "")
    if profile_id:
        profile = profiles.get(profile_id)
        if profile is None or profile.tool != "http-generic":
            logger.warning("http-generic profile 未找到或 tool 不符（跳过装配）: id=%s root=%s", profile_id, root)
        else:
            adapters["http-generic"] = HttpServiceAdapter(
                profile,
                base_url=getattr(settings, "agent_http_generic_base_url", ""),
                api_key=getattr(settings, "agent_http_generic_api_key", ""),
                model=getattr(settings, "agent_http_generic_model", ""),
                session_mapper=PgAdapterSessionMapper(session_factory, adapter="http-generic"),
            )

    cli_profile_id = getattr(settings, "agent_cli_generic_profile", "")
    if cli_profile_id:
        profile = profiles.get(cli_profile_id)
        if profile is None or profile.tool != "cli-generic":
            logger.warning("cli-generic profile 未找到或 tool 不符（跳过装配）: id=%s root=%s", cli_profile_id, root)
        else:
            adapters["cli-generic"] = CliJsonlAdapter(profile)
    return adapters
