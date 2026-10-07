"""intent 金标装载与契约校验（datasets/<version>.jsonl；schema 唯一事实源）。

字段契约（build_golden_v0.py 生成器同源，tests/benchmarks 锁定）：
    id / query / expected_action / ambiguity_level / notes / expectation
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

AmbiguityLevel = Literal["clear", "ambiguous", "need_clarification", "out_of_scope"]
Expectation = Literal["map", "clarify", "reject"]

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"

_REQUIRED_FIELDS = ("id", "query", "expected_action", "ambiguity_level", "notes", "expectation")


@dataclass(slots=True, frozen=True)
class GoldenItem:
    """单条金标（frozen 值对象；expected_action=None 仅越界条合法）。"""

    id: str
    query: str
    expected_action: str | None
    ambiguity_level: AmbiguityLevel
    notes: str
    expectation: Expectation


def load_dataset(version: str = "v0") -> list[GoldenItem]:
    """装载并校验金标集（schema/分布/词表三重校验，漂移即失败——rag corpus loader 同纪律）。"""
    from benchmarks.suites.intent.action_catalog import build_action_catalog
    from benchmarks.suites.intent.datasets.build_golden_v0 import validate

    path = DATASETS_DIR / f"golden_{version}.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"金标集不存在: {path}")
    raw: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        missing = [f for f in _REQUIRED_FIELDS if f not in item]
        if missing:
            raise ValueError(f"{path.name}:{line_no} 缺字段 {missing}")
        raw.append(item)
    valid_actions = {e.name for e in build_action_catalog()}
    validate(raw, valid_actions=valid_actions)  # 数量/分布/词表/期望一致性/id 连续全查
    return [
        GoldenItem(
            id=item["id"],
            query=item["query"],
            expected_action=item["expected_action"],
            ambiguity_level=item["ambiguity_level"],
            notes=item["notes"],
            expectation=item["expectation"],
        )
        for item in raw
    ]
