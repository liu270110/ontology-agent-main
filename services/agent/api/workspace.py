"""L2 会话工作区面板路由（docs/架构设计/31 篇后端 API 需求 #1/#2/#5/#7；画框23 后端 M1）。

端点（api/01 登记册回填随文档批）：
    GET  /sessions/{sid}/workspace/tree   会话工作目录层级树（空目录=空树）
    GET  /sessions/{sid}/workspace/file   只读单文件（≤1MB 内联；二进制 415/3004）
    POST /sessions/{sid}/terminal/exec    受限只读终端（白名单 4001 拒绝；无 shell）
    GET  /sessions/{sid}/resources        会话产物资源列表（M1 顶层产出扫描）

形态（31 篇「沙箱集成」的 M1 最小替代）：会话工作目录=统一配置层
``Settings.workspace_root`` 下 ``{session_id}`` 子目录（宿主直读；20 篇沙箱 daemon
代理随沙箱批次切换）。安全面全量收口在 business/workspace_panel.py（路径监狱/
白名单/超时/截断），本层只做依赖装配、错误体转译与线程池包装（终端子进程与目录
遍历为阻塞执行，事件循环内直调会拖死 SSE——starlette run_in_threadpool 纪律）。

信封=裸 DTO/{items}（api 层既有形态；前端 apiFetch 无 code 字段视为裸数据放行，
frontend/src/api/client.ts 双形态兼容 2026-09-28 批）。
会话存在性校验省略：工作区面纯文件系统导向（目录不存在=空树/空列表语义），sid 为
uuid.UUID 强类型路径参数防注入；scope 门禁=api/01 §5.2 既有 session:read/write。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from services.agent.api.deps import SessionReadDep, SessionWriteDep
from services.agent.api.schemas.workspace import (
    TerminalExecIn,
    WsExecOut,
    WsFileOut,
    WsResourceListOut,
    WsTreeOut,
)
from services.agent.business.workspace_panel import (
    WorkspacePanelError,
    session_resources,
    terminal_exec,
    workspace_file,
    workspace_tree,
)
from services.platform.errors import GatewayError

router = APIRouter(prefix="/sessions", tags=["workspace"])

_OutT = TypeVar("_OutT")


def _workspace_base(request: Request) -> Path:
    """工作区根解析（统一配置层唯一入口）：``Settings.workspace_root``，未配置=禁用面。"""
    settings: Any = getattr(request.app.state, "settings", None)
    root = getattr(settings, "workspace_root", None) if settings is not None else None
    if not root:
        raise GatewayError(5004, "工作区未启用（OA_WORKSPACE_ROOT 未配置）", status_code=503)
    return Path(root)


WorkspaceBaseDep = Annotated[Path, Depends(_workspace_base)]


async def _panel_call(out_type: type[_OutT], func: Any, *args: Any) -> _OutT:
    """线程池执行面板业务 + 结构化错误转统一错误体（WorkspacePanelError→GatewayError）。"""
    try:
        return out_type.model_validate(await run_in_threadpool(func, *args))
    except WorkspacePanelError as exc:
        raise GatewayError(exc.code, exc.message, status_code=exc.status_code) from exc


@router.get("/{session_id}/workspace/tree", summary="会话工作区文件树（31 篇 #1）")
async def get_workspace_tree(session_id: uuid.UUID, principal: SessionReadDep, base: WorkspaceBaseDep) -> WsTreeOut:
    _ = principal  # scope 门禁在依赖内完成；主体经审计中间件统一留痕
    return await _panel_call(WsTreeOut, workspace_tree, base, session_id)


@router.get("/{session_id}/workspace/file", summary="会话工作区文件读取（31 篇 #2，只读）")
async def get_workspace_file(
    session_id: uuid.UUID,
    principal: SessionReadDep,
    base: WorkspaceBaseDep,
    path: Annotated[str, Query(min_length=1, max_length=2_048)],
) -> WsFileOut:
    _ = principal
    return await _panel_call(WsFileOut, workspace_file, base, session_id, path)


@router.post("/{session_id}/terminal/exec", summary="会话受限终端执行（31 篇 #5，只读白名单）")
async def post_terminal_exec(
    session_id: uuid.UUID, body: TerminalExecIn, principal: SessionWriteDep, base: WorkspaceBaseDep
) -> WsExecOut:
    _ = principal
    return await _panel_call(WsExecOut, terminal_exec, base, session_id, body.command)


@router.get("/{session_id}/resources", summary="会话资源列表（31 篇 #7，M1 顶层产出扫描）")
async def get_session_resources(
    session_id: uuid.UUID, principal: SessionReadDep, base: WorkspaceBaseDep
) -> WsResourceListOut:
    _ = principal
    return await _panel_call(WsResourceListOut, session_resources, base, session_id)
