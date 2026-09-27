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
