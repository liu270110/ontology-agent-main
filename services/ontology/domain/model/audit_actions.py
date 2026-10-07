"""ontology 审计动作族（ONT-1.6）：动作字符串单点常量（L4 纯枚举，零框架依赖）。

动作族枚举（契约 06 篇 §ONT-1.6，统一 `ontology.` 家族前缀）写平台审计表 audit_logs（只追加）；
写入助手（repo 级同事务裸 SQL）在 services/ontology/data/audit_actions.py——本模块只定词汇，
供 L2/L3/L6 同源引用（rsi/audit.py 动作常量先例）。

口径注记（登记偏离）：ONT-1.4 行内写作 `ontology.usage_guard_triggered`，§ONT-1.6 家族枚举
写作 `usage.guard_triggered`（带家族前缀即 `ontology.usage.guard_triggered`）——本单点常量采
§ONT-1.6 家族前缀口径，动作段与契约枚举逐字一致（见批次 gaps 登记）。
"""

from __future__ import annotations

from enum import StrEnum


class OntologyAuditAction(StrEnum):
    """11 个动作（§ONT-1.6 枚举逐字，家族前缀 ontology.）；audit_logs.action String(64) 容量已核。"""

    VERSION_PUBLISHED = "ontology.version_published"
    CHANGESET_SUBMITTED = "ontology.changeset.submitted"
    CHANGESET_APPROVED = "ontology.changeset.approved"
    CHANGESET_PUBLISHED = "ontology.changeset.published"
    CHANGESET_REJECTED = "ontology.changeset.rejected"
    CHANGESET_ROLLED_BACK = "ontology.changeset.rolled_back"
    ELEMENT_WITHDRAWN = "ontology.element.withdrawn"
    DEFINITION_SUPERSEDED = "ontology.definition.superseded"
    CRITERION_CHANGED = "ontology.criterion.changed"
    USAGE_GUARD_TRIGGERED = "ontology.usage.guard_triggered"
    PROPOSAL_DECLINED = "ontology.proposal.declined"
