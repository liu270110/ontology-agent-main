# -*- coding: utf-8 -*-
"""LoRA 冒烟（03 决议默认训练路线：嵌入表冻结，r=16，lr 1e-4）。

与 smoke_train.py（全参对照）同数据同步数，只对比显存峰值与耗时：
  - 实测全参 bf16 batch2 = 5.41 GiB（04 日志）；
  - 预期 LoRA ≈ 2.5 GiB（专家估计），嵌入表 192.5M 参数自动冻结。

用法：uv run python src/onto_train/smoke_train_lora.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from onto_train.tokenizer_utils import load_domain_dict  # noqa: E402

MODEL_ID = "urchade/gliner_multi-v2.1"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-size", type=int, default=2)
    ap.add_argument("--steps", type=int, default=10)
    ap.add_argument("--rank", type=int, default=16)
    args = ap.parse_args()

    use_gpu = torch.cuda.is_available()
    print(f"[env] torch={torch.__version__} cuda_available={use_gpu}")

    load_domain_dict()
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

    from gliner import GLiNER
    from gliner.data_processing.collator import SpanDataCollator
    from gliner.training import Trainer, TrainingArguments
    from peft import LoraConfig, get_peft_model

    data_file = ROOT / "data" / "positives.jsonl"
    train_data = [
        {"tokenized_text": s["tokenized_text"], "ner": s["ner"]}
        for s in (json.loads(l) for l in data_file.read_text(encoding="utf-8").splitlines())
    ]
    print(f"[data] {len(train_data)} 条训练样本")

    model = GLiNER.from_pretrained(MODEL_ID)
    if use_gpu:
        model = model.cuda()

    config = getattr(model, "config", None) or model.model.config
    processor = getattr(model, "data_processor", None)
    collator = SpanDataCollator(config, processor)

    lora_cfg = LoraConfig(
        r=args.rank,
        lora_alpha=2 * args.rank,
        lora_dropout=0.05,
        bias="none",
        target_modules="all-linear",  # 全部 Linear；嵌入表(192.5M, 67%)自动冻结
        exclude_modules=r".*span_rep.*|.*projection.*",  # 抽取小头全训，不挂 LoRA
    )
    model = get_peft_model(model, lora_cfg)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"[lora] r={args.rank} 可训练参数 {trainable / 1e6:.1f}M / {total / 1e6:.1f}M ({trainable / total:.1%})")

    targs = TrainingArguments(
        output_dir=str(ROOT / "runs" / "smoke-lora"),
        max_steps=args.steps,
        per_device_train_batch_size=args.batch_size,
        learning_rate=1e-4,  # LoRA 用大学习率（03 决议 §4）
        bf16=use_gpu,
        logging_steps=1,
        save_strategy="no",
        report_to="none",
        seed=42,
        remove_unused_columns=False,
        dataloader_num_workers=0,
        use_cpu=not use_gpu,
    )

    trainer = Trainer(model=model, args=targs, train_dataset=train_data, data_collator=collator)

    if use_gpu:
        torch.cuda.reset_peak_memory_stats()

    print(f"[smoke] LoRA {args.steps} 步冒烟（bf16, batch={args.batch_size}）")
    t0 = time.perf_counter()
    trainer.train()
    dt = time.perf_counter() - t0

    print(f"[smoke] 完成：{dt:.1f}s（{dt / args.steps:.2f}s/步）")
    if use_gpu:
        peak = torch.cuda.max_memory_allocated() / 1024**3
        print(f"[smoke] torch 峰值显存: {peak:.2f} GiB（全参对照 5.41 GiB）")
        print(f"[smoke] 结论: {'PASS' if peak < 5.41 else '未优于全参，检查 exclude/嵌入冻结是否生效'}")


if __name__ == "__main__":
    main()
