"""ORM 聚合注册表：import 各模块 ORM 使全部表入 Base.metadata（Alembic env 唯一聚合点）。

模块轴裁决（2026-09-27）后表定义按模块私有（services.<m>.data.orm）；platform→module 依赖
仅本文件与 platform/db/uow.py 两处组合点，已在 pyproject importlinter 白名单（TODO(M4) 收口）。
"""

from services.agent.data.orm import (  # noqa: F401
    Agent,
    AgentAdapter,
    Message,
    Run,
    Session,
    Task,
    TaskEvent,
)
from services.iam.data.orm import (  # noqa: F401
    ApiKey,
    AuditLog,
    Role,
    Tenant,
    User,
    UserRole,
)
from services.kb.data.orm import (  # noqa: F401
    Document,
    DocumentChunk,
    EvaluationResult,
    EvaluationRun,
    KbCollection,
    KbFact,
    KbPipelineStep,
)
from services.memory.data.orm import MemoryL2Fact  # noqa: F401
from services.ontology.data.orm import (  # noqa: F401
    Axiom,
    OntoClass,
    Ontology,
    OntologyChangeset,
    OntologyVersion,
    OntoProperty,
    Rule,
)
from services.platform.llm.orm import LlmCall  # noqa: F401
from services.plugin.data.orm import (  # noqa: F401
    PluginORM,
    PluginVersionORM,
    ToolInvocationORM,
    ToolORM,
)
from services.review.data.orm import ReviewTicket  # noqa: F401
from services.sandbox.data.orm import (  # noqa: F401
    EgressPolicy,
    SandboxEvent,
    SandboxInstance,
    SandboxProfile,
    SandboxSnapshot,
)
from services.writeback.data.orm import (  # noqa: F401
    OutboxEventORM,
    WritebackLedgerORM,
)
