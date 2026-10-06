"""mcp 管理域仓储（L6；查询编排与状态推进在此，路由只做门禁+DTO 投影——admin_repo 同构）。

租户纪律：一切查询带 tenant_id（06 篇 §4 行级隔离）。事务：常规写由请求级会话
（deps.get_session）成功提交；唯一例外=record_probe_failure 在异常路径先显式 commit
（refresh 失败要落 failing 状态再上抛 5003，否则被 get_session 回滚吞掉）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.agent.data.orm import Agent
from services.iam.data.orm import User
from services.mcp.data.orm import McpServerORM, McpToolORM

_PROBES_WINDOW = 24  # mock probes_24h 环形窗


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- 查询


async def list_servers(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[McpServerORM]:
    """Server 列表（mock unshift 语义=最新在前；uuid7 时间有序，created_at 倒序即稳定）。"""
    rows = await db.execute(
        select(McpServerORM)
        .where(McpServerORM.tenant_id == tenant_id)
        .order_by(McpServerORM.created_at.desc(), McpServerORM.id.desc())
    )
    return list(rows.scalars())


async def get_server(db: AsyncSession, *, tenant_id: uuid.UUID, server_id: uuid.UUID) -> McpServerORM | None:
    """单行（跨租户不命中=None，路由层同口径 404 防枚举——admin_repo.get_model_channel 同款）。"""
    row = await db.get(McpServerORM, server_id)
    if row is None or row.tenant_id != tenant_id:
        return None
    return row


async def server_name_taken(db: AsyncSession, *, tenant_id: uuid.UUID, name: str) -> bool:
    row = await db.execute(
        select(McpServerORM.id).where(McpServerORM.tenant_id == tenant_id, McpServerORM.name == name).limit(1)
    )
    return row.scalar_one_or_none() is not None


async def tools_of_server(db: AsyncSession, *, server_id: uuid.UUID) -> list[McpToolORM]:
    """工具缓存行（登记序=发现序：created_at/id 升序，刷新 upsert 不打乱既有顺序）。"""
    rows = await db.execute(
        select(McpToolORM)
        .where(McpToolORM.server_id == server_id)
        .order_by(McpToolORM.created_at.asc(), McpToolORM.id.asc())
    )
    return list(rows.scalars())


async def get_tool(db: AsyncSession, *, tenant_id: uuid.UUID, tool_id: str) -> McpToolORM | None:
    row = await db.execute(
        select(McpToolORM).where(McpToolORM.tenant_id == tenant_id, McpToolORM.tool_id == tool_id).limit(1)
    )
    return row.scalar_one_or_none()


async def user_display_name(db: AsyncSession, *, user_id: uuid.UUID) -> str:
    """登记人展示名（permission_requests requester 解析先例：display_name→username→'unknown'）。"""
    user = await db.get(User, user_id)
    if user is None:
        return "unknown"
    return user.display_name or user.username or user.email.split("@")[0]


async def affected_agents_count(db: AsyncSession, *, tenant_id: uuid.UUID, server_name: str) -> int:
    """在用 Agent 提示计数（下架级联提示 X-Affected-Agents；config JSONB 文本含 server 名即计——
    mock 静态口径的 live 只读聚合近似，admin_repo 跨面聚合先例同款，提示位非权威引用图）。"""
    row = await db.execute(
        select(func.count())
        .select_from(Agent)
        .where(Agent.tenant_id == tenant_id, cast(Agent.config, String).like(f"%{server_name}%"))
    )
    return int(row.scalar_one() or 0)


# ---------------------------------------------------------------- 写入


async def create_server_with_tools(
    db: AsyncSession, *, server: McpServerORM, tools: list[McpToolORM]
) -> McpServerORM:
    db.add(server)
    for tool in tools:
        db.add(tool)
    await db.flush()
    await db.refresh(server)
    return server


async def set_tool_enabled(db: AsyncSession, *, tool: McpToolORM, enabled: bool) -> McpToolORM:
    tool.enabled = enabled
    tool.updated_at = _now()
    await db.flush()
    return tool


async def refresh_tool_cache(
    db: AsyncSession, *, server: McpServerORM, remote_tools: list[dict[str, Any]]
) -> list[McpToolORM]:
    """刷新工具缓存（按远端 name 对账 upsert）：

    - 既有行：保留 adopted/enabled（纳管与审核决策不因刷新丢失），更新 desc/write/read_only；
    - 新增行：adopted=false、enabled=false 起步（外部默认不可信，07 §1）；
    - 远端已消失行：删除（缓存对账远端真集）。
    返回刷新后的全量行（登记序稳定）。
    """
    from services.mcp.business.probe import derive_tool_flags, persisted_tool_id  # 局部防环

    existing = {row.name: row for row in await tools_of_server(db, server_id=server.id)}
    seen: set[str] = set()
    for remote in remote_tools:
        name = str(remote.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        write, read_only = derive_tool_flags(dict(remote.get("annotations") or {}))
        row = existing.get(name)
        if row is None:
            db.add(
                McpToolORM(
                    tenant_id=server.tenant_id,
                    server_id=server.id,
                    tool_id=persisted_tool_id(server.name, name),
                    name=name,
                    desc=str(remote.get("description") or ""),
                    write=write,
                    read_only=read_only,
                    adopted=False,
                    enabled=False,
                )
            )
        else:
            row.desc = str(remote.get("description") or "")
            row.write = write
            row.read_only = read_only
            row.updated_at = _now()
    vanished = [row for name, row in existing.items() if name not in seen]
    for row in vanished:
        await db.delete(row)
    await db.flush()
    rows = await tools_of_server(db, server_id=server.id)
    server.adopted_count = sum(1 for r in rows if r.adopted)  # 纳管数随对账重算（勾选行被远端下线则减）
    await db.flush()
    return rows


async def record_probe_success(
    db: AsyncSession,
    *,
    server: McpServerORM,
    latency_ms: int,
    protocol: str,
    server_version: str,
    discovered_count: int,
) -> None:
    """探测成功推进：healthy + 环形窗追加（mock refresh 同口径：status/latency/last_probe/discovered_count）。"""
    server.status = "healthy"
    server.latency_ms = latency_ms
    server.consecutive_failures = 0
    server.protocol = protocol or server.protocol
    server.server_version = server_version or server.server_version
    server.discovered_count = discovered_count
    server.last_probe = _now()
    server.probes_24h = (*(server.probes_24h or []), {"ok": True})[-_PROBES_WINDOW:]
    server.updated_at = server.last_probe
    await db.flush()


async def record_probe_failure(db: AsyncSession, *, server: McpServerORM) -> None:
    """探测失败推进：failing + 连败计数 + 环形窗追加；显式 commit（异常路径不被请求级回滚吞掉）。"""
    server.status = "failing"
    server.consecutive_failures += 1
    server.last_probe = _now()
    server.probes_24h = (*(server.probes_24h or []), {"ok": False})[-_PROBES_WINDOW:]
    server.updated_at = server.last_probe
    await db.flush()
    await db.commit()


async def delete_server(db: AsyncSession, *, server: McpServerORM) -> int:
    """下架：级联其工具行（DB ON DELETE CASCADE；返回删除工具数供 X-Removed-Tools 头）。"""
    removed = await db.execute(delete(McpToolORM).where(McpToolORM.server_id == server.id))
    await db.delete(server)
    await db.flush()
    return int(getattr(removed, "rowcount", 0) or 0)
