"""S1 正式训练（03 决议 §5：LoRA 默认路线 + 监督多任务微调）。

数据：manifest 划分的 train 集（模板 + LLM 改写正例），golden dev 严格排除。
配置：LoRA r=16（all-linear，嵌入表冻结，span_rep/projection 全训），
bf16，lr 1e-4，batch 8×accum 4（有效 32），10 epoch（<5k 条经验值）。
产出：runs/s1_model（merge_and_unload 后整模保存——GLiNER.from_pretrained
不认 adapter，必须合并保存；附带 tokenizer 以便直接加载）。

用法：uv run python src/onto_train/train_s1.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from onto_train.model_loader import load_gliner  # noqa: E402
from onto_train.tokenizer_utils import load_domain_dict  # noqa: E402

MODEL_ID = "urchade/gliner_multi-v2.1"
OUT_DIR = ROOT / "runs" / "s1_model"


def main() -> None:
    load_domain_dict()
    from gliner.data_processing.collator import SpanDataCollator
    from gliner.training import Trainer, TrainingArguments
    from peft import LoraConfig, get_peft_model

    manifest = json.loads((ROOT / "data" / "manifest.json").read_text(encoding="utf-8"))
    dev_ids = set(manifest["splits"]["golden_dev_ids"])
    print(f"[data] golden dev {len(dev_ids)} 条将严格排除")

    rows = []
    for name in ("positives.jsonl", "positives_llm.jsonl"):
        p = ROOT / "data" / name
        rows += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]
    train = [{"tokenized_text": r["tokenized_text"], "ner": r["ner"]} for r in rows if r["id"] not in dev_ids]
    print(f"[data] train {len(train)} 条（正例总数 {len(rows)}，dev {len(rows) - len(train)}）")

    model = load_gliner(MODEL_ID).cuda()
    config = getattr(model, "config", None) or model.model.config
    processor = getattr(model, "data_processor", None)
    collator = SpanDataCollator(config, processor)

    model = get_peft_model(
        model,
        LoraConfig(
            r=16,
            lora_alpha=32,
            lora_dropout=0.05,
            bias="none",
            target_modules="all-linear",
            exclude_modules=r".*span_rep.*|.*projection.*",
        ),
    )
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"[lora] 可训练 {trainable / 1e6:.1f}M")

    targs = TrainingArguments(
        output_dir=str(ROOT / "runs" / "s1_ckpt"),
        num_train_epochs=10,
        per_device_train_batch_size=8,
        gradient_accumulation_steps=4,
        learning_rate=1e-4,
        bf16=True,
        logging_steps=10,
        save_strategy="no",
        report_to="none",
        seed=20260926,
        remove_unused_columns=False,
        dataloader_num_workers=0,
    )
    trainer = Trainer(model=model, args=targs, train_dataset=train, data_collator=collator)

    torch.cuda.reset_peak_memory_stats()
    t0 = time.perf_counter()
    trainer.train()
    dt = time.perf_counter() - t0
    peak = torch.cuda.max_memory_allocated() / 1024**3
    print(f"[train] 完成 {dt:.0f}s，峰值 {peak:.2f} GiB")

    merged = model.merge_and_unload()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(OUT_DIR)
    tok = getattr(processor, "tokenizer", None)
    if tok is not None:
        tok.save_pretrained(OUT_DIR)
    print(f"[save] 合并模型 -> {OUT_DIR}")

    # 立即回载 sanity：一条真实语句的预测
    reloaded = load_gliner(str(OUT_DIR)).cuda().eval()
    demo = "GB/T 31486-2024由中国电力企业联合会发布，第5章规定了电池包的容量恢复能力。"
    from onto_train.tokenizer_utils import to_gliner_text

    ents = reloaded.predict_entities(
        to_gliner_text(demo),
        [
            item["label_en"]
            for g in ("ob2_top_classes", "gbt_entity_types")
            for item in json.loads((ROOT / "configs" / "labels.json").read_text(encoding="utf-8"))[g]["labels"]
        ],
        threshold=0.4,
    )
    print("[sanity] 回载预测:", [(e["text"].replace(" ", ""), e["label"], round(e["score"], 2)) for e in ents])


if __name__ == "__main__":
    main()
