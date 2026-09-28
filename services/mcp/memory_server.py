"""MCP 工具出口·记忆域三只读工具（06 篇 §6；FastMCP 独立实例，v1 stdio 可测形态）。

落位：services/mcp/server.py 已是平台能力注册表出口（api/03 §3 七 tool + PDP/审计），本模块
按 M4 计划 3 任务 4 以独立 FastMCP 实例并存；组合根挂载 TODO（与 worker/gateway lifespan 统一）。
依赖经模块级 provider 惰性装配（与网关 get_memory_service 同款模式）：get_repo 为装配接缝，
测试以 monkeypatch 替换；协议形状 = records_repo.MemoryRepository（get/search_keyword）。

红线：MCP annotations 不作授权依据（05 篇）——本出口不做任何授权判定，仅提供只读工具；
write/forget（06 §6 表）涉写入与软删，待审批接续任务经候选队列 + IAM scope 落地，v1 不暴露。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from services.ontology.core.mem_tbox import validate_mem_record

if TYPE_CHECKING:
    from services.memory.data.repositories.records_repo import MemoryRepository

mcp = FastMCP("ontology-agent-memory")

_DEV_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def get_repo() -> MemoryRepository:
    """真实装配接缝（组合根替换）；v1 默认未装配即抛错。"""
    raise RuntimeError("memory repo not wired for MCP")


def _tenant() -> uuid.UUID:
    # TODO(M1)：MCP 会话级租户解析（api_keys scope）；v1 dev 占位
    return _DEV_TENANT_ID


async def memory_search(query: str, limit: int = 5) -> dict[str, Any]:
    """按关键词检索记忆记录（RRF 关键词通道；只读）。"""
    repo = get_repo()
    records = await repo.search_keyword(_tenant(), text_q=query, limit=limit)
    return {
        "records": [
            {
                "id": str(r.id),
                "content": r.content,
                "record_type": str(r.record_type),
                "subject_iri": r.subject_iri,
                "confidence": r.confidence,
            }
            for r in records
        ]
    }


async def memory_read(record_id: str) -> dict[str, Any]:
    """读单条记忆记录（不存在返回 found=false；只读）。"""
    repo = get_repo()
    rec = await repo.get(_tenant(), uuid.UUID(record_id))
    if rec is None:
        return {"found": False}
    return {
        "found": True,
        "record": {
            "id": str(rec.id),
            "content": rec.content,
            "record_type": str(rec.record_type),
            "subject_iri": rec.subject_iri,
            "confidence": rec.confidence,
            "state": str(rec.state),
        },
    }


def ontology_validate(payload: dict[str, Any]) -> dict[str, Any]:
    """mem 候选 SHACL 校验（返回 valid + violations 清单；同步 rdflib 栈直调；只读）。"""
    violations = validate_mem_record(payload)
    return {"valid": not violations, "violations": violations}


# 注册为 MCP tool：fastmcp 的 @mcp.tool 会以 FunctionTool 遮蔽原函数名，此处显式注册
# 并弃用返回值，保留模块级原函数可直调（测试契约：工具函数直调 + monkeypatch 依赖）。
mcp.tool(memory_search)
mcp.tool(memory_read)
mcp.tool(ontology_validate)
