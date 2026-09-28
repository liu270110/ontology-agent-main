"""S1 双指标验收（03 决议 §0.9）：

  指标① 域内：golden dev span-F1，微调后 ≥ 零样本基线 + 10pt
  指标② 遗忘：通用零样本 span-F1（人名/机构/地点/时间/产品），回落 ≤ 3pt

两个模型（零样本基线 / S1 微调）各自在阈值 0.30~0.55 扫描取最优 F1
（红队警告过微调后阈值漂移，给双方同等的阈值自由度才是公平验收）。

用法：uv run python src/onto_train/eval_s1.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from onto_train.model_loader import load_gliner  # noqa: E402
from onto_train.tokenizer_utils import load_domain_dict, tokenize_with_spans  # noqa: E402

THRESHOLDS = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55]

# 通用遗忘检查集：与训练域无关的日常语句，通用标签（不在注册表内）
GENERIC_LABELS = ["person", "organization", "location", "time", "product"]
GENERIC_SENTS = [
    ("张伟在北京的腾讯公司工作了五年。", [("张伟", "person"), ("北京", "location"), ("腾讯公司", "organization")]),
    ("李娜将于明天上午抵达上海浦东机场。", [("李娜", "person"), ("明天上午", "time"), ("上海浦东机场", "location")]),
    ("华为发布了新款手机。", [("华为", "organization")]),
    ("王教授在清华大学教了二十年书。", [("王教授", "person"), ("清华大学", "organization")]),
    ("2024年奥运会在巴黎举行。", [("2024年", "time"), ("巴黎", "location")]),
    ("小王下周三去深圳出差。", [("小王", "person"), ("下周三", "time"), ("深圳", "location")]),
    ("阿里巴巴的总部位于杭州。", [("阿里巴巴", "organization"), ("杭州", "location")]),
    ("这部电影是张艺谋导演的。", [("张艺谋", "person")]),
    ("周杰伦的演唱会在南京奥体中心举行。", [("周杰伦", "person"), ("南京奥体中心", "location")]),
    ("刘备在成都建立了蜀汉。", [("刘备", "person"), ("成都", "location")]),
    ("京东的物流次日就能送到广州。", [("京东", "organization"), ("广州", "location")]),
    ("马斯克创办了 SpaceX 公司。", [("马斯克", "person"), ("SpaceX", "organization")]),
    ("她昨天买了一部新手机。", [("昨天", "time")]),
    ("钱学森回国时经过了香港。", [("钱学森", "person"), ("香港", "location")]),
    ("下个月发布的新品叫 Galaxy S。", [("下个月", "time"), ("Galaxy S", "product")]),
]


def load_sets():
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text(encoding="utf-8"))
    dev_ids = set(manifest["splits"]["golden_dev_ids"])
    labels_cfg = json.loads((ROOT / "configs" / "labels.json").read_text(encoding="utf-8"))
    domain_labels = [l["label_en"] for g in ("ob2_top_classes", "gbt_entity_types") for l in labels_cfg[g]["labels"]]

    rows = []
    for name in ("positives.jsonl", "positives_llm.jsonl"):
        rows += [json.loads(l) for l in (ROOT / "data" / name).read_text(encoding="utf-8").splitlines()]
    dev = [r for r in rows if r["id"] in dev_ids]

    generic = []
    for text, spans in GENERIC_SENTS:
        toks = [w for w, _, _ in tokenize_with_spans(text)]
        ner = []
        for sub, lab in spans:
            cs = text.index(sub)
            hits = [i for i, (_, s, e) in enumerate(tokenize_with_spans(text)) if s < cs + len(sub) and e > cs]
            ner.append([hits[0], hits[-1], lab])
        generic.append({"tokenized_text": toks, "ner": ner})
    return dev, domain_labels, generic


def spaced_offsets(tokens: list[str]) -> list[tuple[int, int]]:
    """token 在空格连接文本中的字符区间。"""
    offs, cur = [], 0
    for t in tokens:
        offs.append((cur, cur + len(t)))
        cur += len(t) + 1
    return offs


def predict_token_spans(model, text_tokens, labels, threshold=0.2):
    """低阈值预测一次，返回 (start_tok, end_tok, label, score)；扫阈值靠过滤 score。"""
    spaced = " ".join(text_tokens)
    offs = spaced_offsets(text_tokens)
    out = []
    for e in model.predict_entities(spaced, labels, threshold=threshold):
        cs, ce = e["start"], e["end"]
        hits = [i for i, (s, t) in enumerate(offs) if s < ce and t > cs]
        if hits:
            out.append((hits[0], hits[-1], e["label"], e["score"]))
    return out


def f1_at(preds, gold, t):
    p = {(a, b, c) for a, b, c, s in preds if s >= t}
    g = {(a, b, c) for a, b, c in gold}
    tp = len(p & g)
    prec = tp / len(p) if p else 0.0
    rec = tp / len(g) if g else 0.0
    return 2 * prec * rec / (prec + rec) if prec + rec else 0.0


def eval_model(model, dataset, labels):
    all_preds = [predict_token_spans(model, d["tokenized_text"], labels) for d in dataset]
    golds = [d["ner"] for d in dataset]
    best_f1, best_t = 0.0, 0.0
    for t in THRESHOLDS:
        f1 = sum(f1_at(p, g, t) for p, g in zip(all_preds, golds)) / len(dataset)
        if f1 > best_f1:
            best_f1, best_t = f1, t
    return best_f1, best_t


def main() -> None:
    load_domain_dict()
    dev, domain_labels, generic = load_sets()
    print(f"[data] golden dev {len(dev)} 条 | 通用遗忘集 {len(generic)} 条")

    base = load_gliner("urchade/gliner_multi-v2.1").cuda().eval()
    s1 = load_gliner(str(ROOT / "runs" / "s1_model")).cuda().eval()

    dev_f1_base, t1 = eval_model(base, dev, domain_labels)
    dev_f1_s1, t2 = eval_model(s1, dev, domain_labels)
    gen_f1_base, t3 = eval_model(base, generic, GENERIC_LABELS)
    gen_f1_s1, t4 = eval_model(s1, generic, GENERIC_LABELS)

    print("\n=== S1 双指标验收 ===")
    print(
        f"指标① 域内 golden dev F1: 零样本 {dev_f1_base:.3f}(t={t1}) -> S1 {dev_f1_s1:.3f}(t={t2})"
        f"  提升 {dev_f1_s1 - dev_f1_base:+.3f}  [门槛 +0.10]"
    )
    m1 = dev_f1_s1 - dev_f1_base >= 0.10
    print(
        f"指标② 通用遗忘 F1:      零样本 {gen_f1_base:.3f}(t={t3}) -> S1 {gen_f1_s1:.3f}(t={t4})"
        f"  回落 {gen_f1_base - gen_f1_s1:+.3f}  [门槛 ≤0.03]"
    )
    m2 = gen_f1_base - gen_f1_s1 <= 0.03
    print(
        f"\n结论: {'PASS 双指标达标' if m1 and m2 else 'FAIL ' + ('指标①未达 ' if not m1 else '') + ('指标②未达' if not m2 else '')}"
    )


if __name__ == "__main__":
    main()
