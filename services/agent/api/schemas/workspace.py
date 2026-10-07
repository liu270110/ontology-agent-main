"""L2 网关 DTO：会话工作区面板（31 篇；02 篇 §6——与领域模型严格分离）。

契约源=frontend/src/mocks/handlers.ts「Agent 工作区面板」段 + frontend/src/features/
chat/workspace.ts（WsNode/WsTree/WsFile/WsExecResult/WsResource）；信封=裸 DTO
（api 层单对象端点既有形态，apiFetch 无 code 字段即视为裸数据放行，双形态兼容）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WsNodeOut(BaseModel):
    """文件树节点（目录递归 children；文件带 size/updated_at）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    path: str = Field(min_length=1, max_length=2_048)  # 虚拟路径（/workspace/...，根为 /）
    type: Literal["dir", "file"]
    size: int | None = None
    updated_at: str | None = None  # ISO8601（mock 为相对时间文案，真端点回 ISO）
    children: list[WsNodeOut] | None = None


class WsTreeOut(BaseModel):
    """GET /sessions/{id}/workspace/tree 响应（裸 DTO）。

    recycle_in_minutes v1 恒 None（休眠回收随沙箱生命周期批，20 篇：会话关闭 30 分钟
    后回收）——前端 recycle != null 判空，None 即不渲染倒计时徽标。
    """

    model_config = ConfigDict(extra="forbid")

    recycle_in_minutes: int | None = None
    root: WsNodeOut


class WsFileOut(BaseModel):
    """GET /sessions/{id}/workspace/file 响应（裸 DTO；≤1MB 内联，二进制 415/3004）。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    language: str = Field(min_length=1, max_length=32)  # 预览高亮键（markdown/json/text…）
    content: str


class TerminalExecIn(BaseModel):
    """POST /sessions/{id}/terminal/exec 请求体（mock 契约 {command}）。"""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=2_000)


class WsExecOut(BaseModel):
    """POST /sessions/{id}/terminal/exec 响应（裸 DTO）。

    exit_code 语义：0=成功；非零=命令自身退出码；124=超时终止（5s 上限）；
    126=执行失败（OSError）；127=可执行文件缺失。白名单/参数越界不走本 DTO——
    4001 结构化拒绝（platform/errors TERMINAL_CMD_NOT_ALLOWED）。
    """

    model_config = ConfigDict(extra="forbid")

    command: str
    exit_code: int
    lines: list[str]


class WsResourceOut(BaseModel):
    """会话资源条目（30 篇对象模型；type 四分组，M1 顶层产出=artifact）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)  # ws-<sha256(name)[:12]>（顶层产物）/表行 id
    type: Literal["attachment", "artifact", "ontology_snapshot", "export"]
    name: str = Field(min_length=1, max_length=256)
    size: int
    status: Literal["ready", "processing", "error"]
    uploaded_by: str = Field(min_length=1, max_length=128)  # sandbox / user / agent:<name>
    created_at: str  # ISO8601


class WsResourceListOut(BaseModel):
    """GET /sessions/{id}/resources 响应（裸 {items} 列表——apiFetch 双形态归一同款）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[WsResourceOut]
