"""L4 端口：凭证登记只读查询（批 K5 门 2；方案依据=docs/Agent/13 §10）。

deer-flow required-secrets 三门的门 2 查询侧缩减版：技能登记时逐名校验声明的凭证是否
已在凭证池登记。适配器=services/platform/llm/cred_pool.py CredentialPool.registered
（按名只读查询，不触轮换/冷却状态）。

装配口径（本批）：组合根尚不注入（None）→ 用例层 fail-closed 把声明集全记缺失
（unprovisioned 消费方可感知，不阻断 listed）；待凭证供给批把凭证名登记进池后接线。
"""

from __future__ import annotations

from typing import Protocol


class SecretsQuery(Protocol):
    """凭证登记只读查询协议（结构化鸭子类型；business 只依赖此形状）。"""

    def registered(self, name: str) -> bool:
        """凭证名是否已登记（只读、无副作用）。"""
        ...
