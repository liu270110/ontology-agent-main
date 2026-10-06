"""工具结果 spill（02 §11.2-11，DSH 勘察细节 11 裁决采纳：超大工具结果 → 有界预览 + 指针）。

动机（DSH 同款）：超大工具结果回灌模型上下文=token 爆量 + 注意力稀释；正文落持久存储换
指针，模型只见**有界预览**（保头尾，DSH compaction 保头尾同构）；完整原文经 locator 可取
（审计/恢复/重放读侧）。存储后端经 :class:`SpillStore` 协议注入——内核只认协议（A3 依赖
倒置），MinIO 实现随 M4 公共件批（当前组合根用本地目录实现，ontology 工件同款先例）。

确定性纪律（推理分级宪法）：阈值判定与预览构造为纯函数，无 LLM、无随机；阈值/预览长度
为建议初值（与其他预算同口径，实测冻结）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

from services.agent.domain.model.kernel_actions import ToolResult

logger = logging.getLogger(__name__)

SPILL_THRESHOLD_CHARS = 8_000  # 序列化正文超过即 spill（建议初值，实测冻结）
PREVIEW_HEAD_CHARS = 2_000  # 预览保留头部
PREVIEW_TAIL_CHARS = 500  # 预览保留尾部（DSH 保头尾同构）
# K14-c（docs/Agent/13 §20）：截断标记单一事实源——内核预览 / web fetch 正文截断 /
# fs 工具截断三处同源同格式（headroom §5 CCR 闭环：标记自带 locator，读侧经 spill.get
# 兑换；防孤儿=locator 校验在 SpillStore.get 内收口，不信任调用方转述的指针）。
SPILL_MARKER_FMT = "[spilled: locator={locator}]"
_OMISSION_MARK = "\n…" + SPILL_MARKER_FMT.format(locator="完整原文见 locator 字段；本预览截断") + "…\n"


@runtime_checkable
class SpillStore(Protocol):
    """工具结果溢出存储（组合根注入；MinIO 实现随 M4，本地目录实现见 agent.data）。"""

    async def put(self, key: str, payload: str) -> str:
        """存全文，返回 locator 指针（读侧凭 locator 取回原文）。"""
        ...

    async def get(self, locator: str, *, tenant_id: str) -> str | None:
        """凭 locator 取回原文（K14-a 读侧兑换；spill.get 工具的唯一读入口）。

        语义（实现必须遵守）：非法 locator（路径逃逸/跨租户）→ 抛 ValueError 结构化
        拒绝；读不到（不存在/已归档清理）→ 返回 None（读不到≠非法，两种失败可区分，
        供调用方给模型不同的恢复指引）。tenant_id=调用方租户上下文注入（不从 locator
        猜），归属与逃逸校验在 get 内做——防孤儿防线（headroom §5）。
        """
        ...


def serialize_output(output: dict[str, Any]) -> str:
    """结果序列化（确定性；非 JSON 原生值降级 str，禁异常逃逸）。"""
    return json.dumps(output, ensure_ascii=False, default=str)


async def spill_if_oversized(
    result: ToolResult,
    store: SpillStore | None,
    *,
    key: str,
) -> ToolResult:
    """超大结果 → 有界预览 + locator（异步：正文落 store，模型上下文只进预览）。

    语义：
    - 未超阈值（或 store 未注入）→ 原样返回（零行为变化）；
    - 超阈值且落盘成功 → output 替换为结构化预览行（locator + 头尾预览 + 原始规模）；
      预览截断标注内嵌同源 locator（K14-c SPILL_MARKER_FMT，模型在预览内即可见兑换指针）；
    - 超阈值但落盘失败（fail-open）→ 仍必须有界：保头尾 + persist_failed 标注（上下文
      防线不因存储故障失守；原文丢失以告警与 original_chars 留痕）。
    """
    if result.output is None or store is None:
        return result
    serialized = serialize_output(result.output)
    if len(serialized) <= SPILL_THRESHOLD_CHARS:
        return result
    try:
        locator = await store.put(key, serialized)
    except Exception as exc:  # noqa: BLE001 ——存储故障 fail-open：有界预览仍回灌（上下文防线不失守）
        logger.warning("工具结果 spill 落盘失败（key=%s）: %s", key, exc)
        oversized: dict[str, Any] = {
            "spilled": False,
            "persist_failed": f"{type(exc).__name__}: {exc}"[:200],
            **build_preview_payload(serialized),
        }
        return result.model_copy(update={"output": oversized})
    oversized = {
        "spilled": True,
        "locator": locator,
        **build_preview_payload(serialized, locator=locator),
    }
    return result.model_copy(update={"output": oversized})


def build_preview_payload(serialized: str, *, locator: str | None = None) -> dict[str, Any]:
    """构造有界预览载荷（头+尾+规模；spill 判定与预览共用，供失败分支与测试对齐）。

    locator 已知时截断标注嵌入同源 SPILL_MARKER_FMT（K14-c）；未知时用无 locator 兜底
    标注（fail-open / 纯预览构造面）。
    """
    return {
        "original_chars": len(serialized),
        "preview": _bounded_preview(serialized, locator=locator),
    }


def _bounded_preview(serialized: str, *, locator: str | None = None) -> str:
    if len(serialized) <= PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS:
        return serialized
    if locator is not None:
        mark = "\n…" + SPILL_MARKER_FMT.format(locator=locator) + "…\n"
    else:
        mark = _OMISSION_MARK
    return serialized[:PREVIEW_HEAD_CHARS] + mark + serialized[-PREVIEW_TAIL_CHARS:]
