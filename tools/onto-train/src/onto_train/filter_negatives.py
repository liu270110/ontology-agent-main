"""公理负例相似度过滤（四步管线第 4 步，03 决议 §2）。

用 bge-m3（本机 Ollama 端点，与平台嵌入契约同模型）计算
sim(负例文本, 干净孪生文本)：相似度 ≥ 阈值（默认 0.9）的才保留——
违规改动足够"表面细微"，模型无法靠表面线索作弊，才是真正的困难负对。

用法：
  uv run python src/onto_train/filter_negatives.py                # 阈值 0.9
  uv run python src/onto_train/filter_negatives.py --threshold 0.85
输出：data/negatives_filtered.jsonl（原 negatives.jsonl 不动，保留血缘）
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"  # 原生通道：/v1/embeddings 兼容层对特定文本会稳定 500
EMBED_MODEL = "bge-m3"  # 平台嵌入契约模型（架构 07 篇定案），本机 Ollama 同款


def embed(texts: list[str]) -> list[list[float]]:
    """批量嵌入（一次请求多条），带 3 次重试（换载竞争下 ollama 会瞬时 500）。"""
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                OLLAMA_EMBED_URL,
                data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())["embeddings"]
        except Exception as err:  # noqa: BLE001
            last_err = err
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"嵌入请求失败（3 次重试后）: {last_err}")


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.9)
    ap.add_argument("--out", default=str(ROOT / "data" / "negatives_filtered.jsonl"))
    args = ap.parse_args()

    src = [json.loads(l) for l in (ROOT / "data" / "negatives.jsonl").read_text(encoding="utf-8").splitlines()]

    kept, dropped, nan_failed = [], 0, 0
    sims_by_kind: dict[str, list[float]] = {}
    for s in src:
        clean = s.get("clean_text", "")
        if not clean:
            dropped += 1
            continue
        try:
            vecs = embed([s["text"], clean])
            sim = round(cosine(vecs[0], vecs[1]), 4)
        except RuntimeError:
            # ollama+bge-m3 对特定文本产出 NaN 向量（服务端 json 拒绝 NaN 报 500，
            # 见 server.log "unsupported value: NaN"）。符号验证已保证真违规，
            # 相似度只是难度过滤器——记 None 默认保留，不丢样本。
            sim = None
            nan_failed += 1
        s["surface_sim"] = sim
        if sim is None:
            kept.append(s)
            continue
        sims_by_kind.setdefault(s["axiom_kind"], []).append(sim)
        if sim >= args.threshold:
            kept.append(s)
        else:
            dropped += 1

    out = Path(args.out)
    with out.open("w", encoding="utf-8") as f:
        for s in kept:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    print(
        f"[filter] 阈值 {args.threshold}：保留 {len(kept)} / {len(src)}（NaN 降级保留 {nan_failed}），剔除 {dropped} -> {out}"
    )
    for kind, sims in sorted(sims_by_kind.items()):
        avg = sum(sims) / len(sims)
        print(f"  {kind:22s} 平均相似度 {avg:.3f}（{'达标' if avg >= args.threshold else '偏易，考虑加噪'}）")


if __name__ == "__main__":
    main()
