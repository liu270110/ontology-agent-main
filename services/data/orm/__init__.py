"""ORM 聚合导出（06 篇 §1）。Alembic autogenerate 从此 import Base。M1=17 表，M2=+10 表。"""

from .agent_session import Agent, AgentAdapter, Message, Run, Session, Task, TaskEvent
from .base import Base
from .identity import ApiKey, Role, Tenant, User, UserRole
from .kb_ontology_audit import AuditLog, Document, KbCollection, Ontology, OntologyVersion
from .m2_semantic_review import (
    Axiom,
    DocumentChunk,
    EvaluationResult,
    EvaluationRun,
    KbPipelineStep,
    OntoClass,
    OntologyChangeset,
    OntoProperty,
    ReviewTicket,
    Rule,
)

__all__ = [
    "Axiom",
    "Agent",
    "AgentAdapter",
    "ApiKey",
    "AuditLog",
    "Base",
    "Document",
    "DocumentChunk",
    "EvaluationResult",
    "EvaluationRun",
    "KbCollection",
    "KbPipelineStep",
    "Message",
    "OntoClass",
    "OntoProperty",
    "Ontology",
    "OntologyChangeset",
    "OntologyVersion",
    "ReviewTicket",
    "Role",
    "Rule",
    "Run",
    "Session",
    "Task",
    "TaskEvent",
    "Tenant",
    "User",
    "UserRole",
]
