"""agent 运行时内核（docs/Agent/02 自研裁决）：A 循环骨架 + B 安全基线 + C 状态账本。

包结构（02 §7 反腐化：内核 import 白名单 = stdlib + pydantic + platform + 本模块 domain）：

- ``extensions``   八扩展点 Protocol（§4.1 契约面，随本实现冻结）；
- ``dispatcher``   A3 扩展点分发器（内核只认 Protocol，能力经注册注入）；
- ``loop``         A1 七阶段主循环编排 + A4 预算终止；
- ``run_context``  单次运行上下文（状态/账本/预算/取消协调聚合）；
- ``grounding``    ①装载＋②组装阶段（供给器取材、绝对预算裁剪、B3 标界）；
- ``execution``    ⑤执行断点（B5 审批路由、B3 标界、B4 出口、C3 租户核验）；
- ``gate_baseline`` B1 门禁基线（只增不替的「基」）；
- ``criteria``     B2 判据求值器（只认外部回执，M3 简化版凭证源=内核账本）；
- ``ledger``       C1 v1 简化账本 + C2 trace/审计记账；
- ``cancellation`` 取消完整性清单（§2.4，5s 超时兜底）；
- ``budget``       A4 token/步数/时长三维预算。
"""

from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.loop import AgentKernel

__all__ = ["AgentKernel", "ExtensionDispatcher"]
