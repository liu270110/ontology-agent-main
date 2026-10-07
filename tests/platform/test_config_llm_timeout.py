# tests/platform/test_config_llm_timeout.py
"""llm_timeout_s Settings 化（2026-10-07 首晋门4 教训收口）：

组合根 `_LLM_TIMEOUT_S=60.0` 硬编码在门4 实测云端端点 8/8 撞 60s 硬超时且 env 不可调
（standards/02 §10.4 晋级台账门4 卡点 + standards/03 §5 经验台账首晋第二窗登记
「超时改 Settings 化(遗留)」）。收口为 Settings.llm_timeout_s：默认 60.0=原硬编码值
零行为变化；ge=10/le=600 边界夹紧；env=OA_LLM_TIMEOUT_S（env_prefix 自动映射）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.platform.config import Settings


def test_llm_timeout_s_缺省默认60_环境变量可覆盖(monkeypatch):
    # Arrange：清 env + 免 .env，防本机配置串扰（显式压过 OA_ 环境变量口径同 compose 测试）
    monkeypatch.delenv("OA_LLM_TIMEOUT_S", raising=False)
    # Act / Assert：缺省=60.0（原组合根硬编码值，零行为变化）
    assert Settings(_env_file=None).llm_timeout_s == 60.0
    # env=OA_LLM_TIMEOUT_S 映射生效（统一配置层默认值→OA_ 环境变量次序，config.py 模块 docstring）
    monkeypatch.setenv("OA_LLM_TIMEOUT_S", "300")
    assert Settings(_env_file=None).llm_timeout_s == 300.0


def test_llm_timeout_s_边界_10下限600上限_越界ValidationError(monkeypatch):
    # Arrange：清 env，隔离本机配置
    monkeypatch.delenv("OA_LLM_TIMEOUT_S", raising=False)
    # Act / Assert：闭区间 [10, 600] 两端放行
    for value in (10.0, 600.0):
        assert Settings(_env_file=None, llm_timeout_s=value).llm_timeout_s == value
    # 越界（<10 / >600）构造期即拒（ValidationError）
    for bad in (9.9, 600.1):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, llm_timeout_s=bad)
