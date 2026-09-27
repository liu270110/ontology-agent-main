"""todo 能力护栏（docs/Agent/06 #5：项数上限/内容长度/状态枚举白名单，deny-by-default）。

todo 是受控申报面而非自由改写面（T2：插件只能申报不能裁决）。规模护栏论证：清单是
模型工作记忆的投影，项数上限 50 与 fs 检索面上限同数量级——超限应收敛/合并或分期申报，
而非整表灌上下文；单项内容只放摘要行（≤500 字符），长文应落工作区文件并在内容里引用
路径（fs 工具族承接）。枚举白名单是 T2 标界的第一道字形闸：清单层不存在 done 之外的
「完成态」，任何枚举外值一律结构化拒绝（错误为模型设计，给可执行修正动作）。
"""

from __future__ import annotations

from services.agent.business.capabilities.todo.models import TodoItemStatus
from services.platform.errors import ErrorCode

TODO_MAX_ITEMS = 50  # 清单项数上限（整表申报重建的规模硬顶）
TODO_MAX_CONTENT_CHARS = 500  # 单项内容字符上限（摘要行语义，长文走工作区文件）
TODO_ITEM_ID_MAX_CHARS = 64  # 显式 item_id 长度上限

_STATUS_WHITELIST = tuple(status.value for status in TodoItemStatus)


class TodoToolError(Exception):
    """todo 能力结构化错误：登记错误码 + 面向模型的消息（修正动作），禁裸异常语义。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_status(raw: object) -> TodoItemStatus:
    """状态枚举白名单（T2 字形闸）：枚举外一律结构化拒绝，消息点名合法三态。"""
    if isinstance(raw, TodoItemStatus):
        return raw
    if isinstance(raw, str):
        try:
            return TodoItemStatus(raw.strip())
        except ValueError:
            pass
    raise TodoToolError(
        ErrorCode.PARAM_INVALID,
        f"status 非法: {raw!r}；只接受 {'/'.join(_STATUS_WHITELIST)}"
        "（done=完成申报，是否生效由内核判据裁决，模型自述不改变权威状态）",
    )


def validated_content(raw: object) -> str:
    """内容护栏：非空 + 长度上限；错误消息给修正动作（摘要行/落文件）。"""
    if not isinstance(raw, str):
        raise TodoToolError(ErrorCode.PARAM_INVALID, f"content 须为字符串，得到 {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise TodoToolError(ErrorCode.PARAM_INVALID, "content 为空白：请给出可执行的清单项描述")
    if len(text) > TODO_MAX_CONTENT_CHARS:
        raise TodoToolError(
            ErrorCode.PARAM_INVALID,
            f"content {len(text)} 字符超过单项上限 {TODO_MAX_CONTENT_CHARS}："
            "清单只放摘要行，长文请写入工作区文件并在内容里引用路径（fs.write 承接）",
        )
    return text


def validated_item_id(raw: object) -> str:
    """显式项 id 护栏：非空字符串 + 长度上限（自动分配 id 为 todo-NN，不经此校验）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise TodoToolError(ErrorCode.PARAM_INVALID, f"item_id 须为非空字符串，得到 {raw!r}")
    item_id = raw.strip()
    if len(item_id) > TODO_ITEM_ID_MAX_CHARS:
        raise TodoToolError(
            ErrorCode.PARAM_INVALID,
            f"item_id {len(item_id)} 字符超过上限 {TODO_ITEM_ID_MAX_CHARS}：请使用短 id（todo_read 可查现有 id）",
        )
    return item_id


def validated_criterion_refs(raw: object, known_ids: frozenset[str]) -> tuple[str, ...]:
    """判据引用白名单：只接受本运行计划判据集内的 id（不在集内=引用了不存在的判据，拒绝）。"""
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise TodoToolError(ErrorCode.PARAM_INVALID, f"criterion_refs 须为字符串数组，得到 {type(raw).__name__}")
    refs: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise TodoToolError(ErrorCode.PARAM_INVALID, f"criterion_refs 含空项: {entry!r}；请给出计划判据 id")
        ref = entry.strip()
        if ref not in known_ids:
            known = "、".join(sorted(known_ids)) if known_ids else "（本运行计划未携带判据）"
            raise TodoToolError(
                ErrorCode.PARAM_INVALID,
                f"判据引用 {ref!r} 不在本运行计划判据集内；可用判据：{known}。"
                "不引用判据的项申报 done 只会记为候选（人工/规划侧裁决）",
            )
        if ref not in refs:
            refs.append(ref)
    return tuple(refs)
