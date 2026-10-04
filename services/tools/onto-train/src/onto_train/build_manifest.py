"""数据集清单 + train/dev 划分（血缘追踪的落地，03 决议 §4 数据血缘行）。

生成 data/manifest.json：
  - 每个数据/配置文件的 sha256（MLflow 接入后直接迁移为血缘表字段）；
  - 标签 schema_hash（labels.json 纪律：本体 changeset 发布需 diff）；
  - 各文件计数与来源分布（template_v0.1 / llm_synth / axiom_negative）；
  - 确定性 train/dev 划分（golden dev = 每隔 1/10 抽样，seed 固定）。

用法：uv run python src/onto_train/build_manifest.py
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def schema_hash(labels: dict) -> str:
    """按 labels.json 约定：四组标签的 iri 排序拼接后取哈希。"""
    iris: list[str] = []
    for g in ("ob2_top_classes", "gbt_entity_types", "gbt_object_properties", "ob2_action_properties"):
        iris.extend(l["iri"] for l in labels[g]["labels"])
    return hashlib.sha256("|".join(sorted(iris)).encode("utf-8")).hexdigest()[:16]


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def source_breakdown(rows: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        src = r.get("provenance", {}).get("source", r.get("axiom_kind", "unknown"))
        out[src] = out.get(src, 0) + 1
    return dict(sorted(out.items()))


def main() -> None:
    labels = json.loads((ROOT / "configs" / "labels.json").read_text(encoding="utf-8"))

    files = {
        "configs/labels.json": ROOT / "configs" / "labels.json",
        "dictionaries/domain_terms.dic": ROOT / "dictionaries" / "domain_terms.dic",
        "data/positives.jsonl": DATA / "positives.jsonl",
        "data/positives_llm.jsonl": DATA / "positives_llm.jsonl",
        "data/negatives.jsonl": DATA / "negatives.jsonl",
        "data/negatives_filtered.jsonl": DATA / "negatives_filtered.jsonl",
    }

    manifest: dict = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "jieba_pinned": "0.42.1",
        "model_id": "urchade/gliner_multi-v2.1",
        "schema_hash": schema_hash(labels),
        "label_stats": labels["stats"],
        "files": {},
        "splits": {},
    }
    for key, path in files.items():
        if path.exists():
            manifest["files"][key] = {"sha256": sha256(path), "rows": None}
        else:
            manifest["files"][key] = {"sha256": None, "rows": None, "note": "尚未生成"}

    # 计数与来源分布
    pos_t = load_jsonl(DATA / "positives.jsonl")
    pos_l = load_jsonl(DATA / "positives_llm.jsonl")
    neg = load_jsonl(DATA / "negatives.jsonl")
    neg_f = load_jsonl(DATA / "negatives_filtered.jsonl")
    manifest["files"]["data/positives.jsonl"]["rows"] = len(pos_t)
    manifest["files"]["data/positives_llm.jsonl"]["rows"] = len(pos_l)
    manifest["files"]["data/negatives.jsonl"]["rows"] = len(neg)
    manifest["files"]["data/negatives_filtered.jsonl"]["rows"] = len(neg_f)
    manifest["provenance"] = {
        "positives": source_breakdown(pos_t + pos_l),
        "negatives": {"all": source_breakdown(neg), "surface_hard": source_breakdown(neg_f)},
    }

    # 确定性 train/dev 划分：dev=golden 验收集（约 10%），训练绝不触碰
    all_pos = pos_t + pos_l
    rng = random.Random(20260926)
    idx = list(range(len(all_pos)))
    rng.shuffle(idx)
    dev_n = max(1, len(idx) // 10)
    dev_idx = set(idx[:dev_n])
    manifest["splits"] = {
        "rule": "seed=20260926 shuffle 后前 1/10 为 golden dev；训练集禁止包含 dev 样本",
        "golden_dev_ids": [all_pos[i]["id"] for i in sorted(dev_idx)],
        "train_count": len(all_pos) - dev_n,
        "dev_count": dev_n,
    }

    out = DATA / "manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[manifest] -> {out}")
    print(
        f"  schema_hash={manifest['schema_hash']} 正例 {len(all_pos)}（dev {dev_n}/train {len(all_pos) - dev_n}）"
        f" 负例 {len(neg)}（表面困难 {len(neg_f)}）"
    )


if __name__ == "__main__":
    main()
