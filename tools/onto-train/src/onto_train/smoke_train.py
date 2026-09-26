# -*- coding: utf-8 -*-
"""10-step 冒烟训练（03 决议 §5.3：nvidia-smi 实测峰值后再定全参/LoRA）。

验证三件事：
  1. gliner 0.2.29 pip 包内置 Trainer 在当前 transformers 版本下能否走通训练路径；
  2. bf16 + batch 2 的显存峰值（8GB 预算红线）；
  3. 10 步内 loss 是否正常下降（数据/标注格式无系统性错误）。

用法：
  python src/onto_train/smoke_train.py                # GPU bf16
  python src/onto_train/smoke_train.py --cpu          # 无 GPU 时语法级验证
"""
from __future__ import annotations

import argparse
import json
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
    ap.add_argument("--cpu", action="store_true")
    ap.add_argument("--batch-size", type=int, default=2)
    ap.add_argument("--steps", type=int, default=10)
    args = ap.parse_args()

    use_gpu = not args.cpu and torch.cuda.is_available()
    print(f"[env] torch={torch.__version__} cuda_available={torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"[env] GPU: {torch.cuda.get_device_name(0)}")

    load_domain_dict()
    from gliner import GLiNER
    from gliner.data_processing.collator import SpanDataCollator
    from gliner.training import Trainer, TrainingArguments

    data_file = ROOT / "data" / "positives.jsonl"
    train_data = [json.loads(l) for l in data_file.read_text(encoding="utf-8").splitlines()]
    train_data = [
        {"tokenized_text": s["tokenized_text"], "ner": s["ner"]} for s in train_data
    ]
    print(f"[data] {len(train_data)} 条训练样本（{data_file.name}）")

    import os

    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    model = GLiNER.from_pretrained(MODEL_ID)
    if use_gpu:
        model = model.cuda()

    config = getattr(model, "config", None) or model.model.config
    processor = getattr(model, "data_processor", None)
    collator = SpanDataCollator(config, processor)

    targs = TrainingArguments(
        output_dir=str(ROOT / "runs" / "smoke"),
        max_steps=args.steps,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=1,
        learning_rate=1e-5,
        bf16=use_gpu,  # 4060 Ada 原生 bf16（03 决议）
        logging_steps=1,
        save_strategy="no",
        report_to="none",
        seed=42,
        remove_unused_columns=False,
        dataloader_num_workers=0,
        use_cpu=not use_gpu,  # transformers 4.17+ / 5.x：no_cuda 已移除
    )

    trainer = Trainer(
        model=model,
        args=targs,
        train_dataset=train_data,
        data_collator=collator,
    )

    if use_gpu:
        torch.cuda.reset_peak_memory_stats()

    print(f"[smoke] 开始 {args.steps} 步冒烟（bf16={use_gpu}, batch={args.batch_size}）")
    t0 = time.perf_counter()
    trainer.train()
    dt = time.perf_counter() - t0

    print(f"[smoke] 完成：{args.steps} 步用时 {dt:.1f}s（{dt / args.steps:.2f}s/步）")
    if use_gpu:
        peak = torch.cuda.max_memory_allocated() / 1024**3
        print(f"[smoke] torch 峰值显存: {peak:.2f} GiB")
        print(f"[smoke] 结论: {'PASS（<7GiB 预算内）' if peak < 7 else '超预算，须降 batch 或转 LoRA'}")


if __name__ == "__main__":
    main()
