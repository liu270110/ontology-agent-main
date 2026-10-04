"""skills.api.schemas：REST DTO（信封全按 api/01 §3.1 {data, meta}，be2 口径）。"""

from services.skills.api.schemas.skill import (
    SkillCreateIn,
    SkillDetailEnvelope,
    SkillLifecycleIn,
    SkillListEnvelope,
    SkillOut,
)

__all__ = [
    "SkillCreateIn",
    "SkillDetailEnvelope",
    "SkillLifecycleIn",
    "SkillListEnvelope",
    "SkillOut",
]
