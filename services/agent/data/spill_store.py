"""工具结果 spill 存储：本地目录实现（agent.data；ontology 工件同款先例）。

`TODO(M4)`：切 MinIO 对象存储（boto3，ontology_repo 同款升级路径）；locator 语义保持
「读侧凭 locator 取回原文」，切换对内核透明（SpillStore 协议不变）。
"""

from __future__ import annotations

from pathlib import Path

from services.agent.business.kernel.spill import SpillStore


class LocalDirSpillStore(SpillStore):  # 显式协议实现（结构化满足亦可）
    """本地目录 spill 存储（组合根按 Settings.task_spill_dir 装配；未配置=spill 关闭）。"""

    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir)

    async def put(self, key: str, payload: str) -> str:
        """key 即相对路径（含租户/run/call 分段）：写 JSON 文件，返回文件路径作 locator。"""
        target = (self._root / key).resolve()
        if self._root.resolve() not in target.parents:  # 路径逃逸防护（key 来自内核构造，防御性断言）
            raise ValueError(f"spill key 越界: {key}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        return str(target)

    async def get(self, locator: str, *, tenant_id: str) -> str | None:
        """凭 locator 取回原文（K14-a 读侧兑换）：逃逸防护与 put 同款 + 租户归属校验。

        - 非法（越界/跨租户/空指针）→ ValueError 结构化拒绝（防孤儿防线在 get 内收口：
          调用方转述的 locator 不可信，root 内且路径含调用租户段才放行）；
        - 读不到（不存在/已归档/IO 失败）→ None（读不到≠非法，语义可区分，见协议注释）。
        """
        if not isinstance(locator, str) or not locator.strip():
            raise ValueError("spill locator 为空")
        root = self._root.resolve()
        target = Path(locator).resolve()
        if root not in target.parents:  # 路径逃逸防护（与 put 同款）
            raise ValueError(f"spill locator 越界: {locator[:200]}")
        if str(tenant_id) not in target.relative_to(root).parts:  # 租户归属：locator 须位于调用租户段之下
            raise ValueError(f"spill locator 跨租户访问被拒: {locator[:200]}")
        try:
            return target.read_text(encoding="utf-8")
        except OSError:
            return None  # 读不到≠非法：原文可能已归档/清理（协议注释语义）
