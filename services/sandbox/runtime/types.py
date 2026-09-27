"""沙箱执行原语（L7 本地定义——分层修复：原属 domain.model.sandbox，L7 禁 import L4）。

与 domain.model.sandbox 的值对象保持字面一致；跨层转换在 L6 仓储完成。
"""

from __future__ import annotations

from enum import StrEnum


class Scenario(StrEnum):
    S0_SESSION = "S0"
    S1_MARKET = "S1"
    S2_PLUGIN = "S2"
    S3_CODE = "S3"
    S4_EVAL = "S4"
    S5_TRAINING = "S5"


class TrustLevel(StrEnum):
    T0_PLATFORM = "T0"
    T1_SIGNED = "T1"
    T2_MARKET = "T2"
    T3_ADVERSARIAL = "T3"
