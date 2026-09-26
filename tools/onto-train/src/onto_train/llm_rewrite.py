# -*- coding: utf-8 -*-
"""LLM 改写管线（Jev 模式：大模型离线造数据，小模型在线判定）。

在模板正例基础上让 LLM 同义改写句子并回传字符跨度，本模块做硬校验后入库：
  - 标签必须在标签注册表 entity 闭集内；
  - 字符跨度必须与回传文本严格对齐（切片相等）；
  - char→token 映射到词跨度（与训练格式一致）。

端点配置（LiteLLM/OpenAI 兼容，走平台模型网关）：
  ONTO_LLM_BASE_URL / ONTO_LLM_MODEL / ONTO_LLM_API_KEY
未配置时 dry-run：打印一个示例 prompt 后退出——绝不用未经校验的数据入库。

用法：
  uv run python src/onto_train/llm_rewrite.py --dry-run
  ONTO_LLM_BASE_URL=... ONTO_LLM_MODEL=... uv run python src/onto_train/llm_rewrite.py --limit 200
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from onto_train.tokenizer_utils import char_span_to_token_span, tokenized_text  # noqa: E402

ENTITY_LABELS: set[str] = set()
for _g in ("ob2_top_classes", "gbt_entity_types"):
    _p = ROOT / "configs" / "labels.json"
    _labels = json.loads(_p.read_text(encoding="utf-8"))[_g]["labels"]
    ENTITY_LABELS.update(l["label_en"] for l in _labels)

PROMPT = """你是训练数据构造器。把下面句子改写成一句自然、多样的中文（标准文档领域），
要求：
1. 保留全部标注片段的语义与边界（可微调措辞，不得增删标注片段）；
2. 输出严格 JSON：{{"text": "...", "spans": [{{"start": 0, "end": 5, "label": "..."}}, ...]}}
   其中 start/end 是改写后文本中的字符偏移（闭开区间），label 只能取：{labels}。

原句：{text}
标注：{spans}"""

VALIDATED = 0
REJECTED = 0


def validate(text: str, spans: list[dict]) -> list | None:
    """硬校验 + char→token 映射。任一不合规返回 None（拒绝入库）。"""
    global VALIDATED, REJECTED
    ner = []
    for sp in spans:
        label = sp.get("label")
        s, e = sp.get("start"), sp.get("end")
        if label not in ENTITY_LABELS or not isinstance(s, int) or not isinstance(e, int):
            REJECTED += 1
            return None
        if not (0 <= s < e <= len(text)) or text[s:e] != text[s:e].strip():
            REJECTED += 1
            return None
        mapped = char_span_to_token_span(text, s, e)
        if mapped is None:
            REJECTED += 1
            return None
        ner.append([mapped[0], mapped[1], label])
    VALIDATED += 1
    return ner


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "data" / "positives_llm.jsonl"))
    args = ap.parse_args()

    base_url = os.environ.get("ONTO_LLM_BASE_URL")
    model = os.environ.get("ONTO_LLM_MODEL")
    api_key = os.environ.get("ONTO_LLM_API_KEY")

    src = [
        json.loads(l)
        for l in (ROOT / "data" / "positives.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    sample = src[0]
    example_prompt = PROMPT.format(
        labels=", ".join(sorted(ENTITY_LABELS)),
        text=sample["text"],
        spans=json.dumps(
            [
                {
                    "start": None,
                    "end": None,
                    "label": lab,
                    "note": "调用方会填入真实字符偏移",
                }
                for _, _, lab in sample["ner"]
            ],
            ensure_ascii=False,
        ),
    )

    if args.dry_run or not (base_url and model and api_key):
        print("[llm-rewrite] dry-run（未配置 ONTO_LLM_* 或显式 --dry-run）")
        print("---- 示例 prompt ----")
        print(example_prompt)
        print("---- 配置端点后：uv run python src/onto_train/llm_rewrite.py --limit 200 ----")
        return

    from openai import OpenAI  # 平台网关 OpenAI 兼容协议（架构锚点 §模型网关）

    client = OpenAI(base_url=base_url, api_key=api_key)
    out = Path(args.out)
    written = 0
    with out.open("w", encoding="utf-8") as f:
        for s in src[: args.limit]:
            spans = []
            # 从 token 跨度还原字符跨度（改写前）
            text = s["text"]
            toks = s["tokenized_text"]
            # 用原文重新映射（tokenized_text 与 text 同源）
            from onto_train.tokenizer_utils import tokenize_with_spans

            tok_spans = tokenize_with_spans(text)
            for i, j, lab in s["ner"]:
                start = tok_spans[i][1]
                end = tok_spans[j][2]
                spans.append({"start": start, "end": end, "label": lab})
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": PROMPT.format(
                            labels=", ".join(sorted(ENTITY_LABELS)),
                            text=text,
                            spans=json.dumps(spans, ensure_ascii=False),
                        ),
                    }
                ],
                temperature=0.9,
                response_format={"type": "json_object"},
            )
            try:
                payload = json.loads(resp.choices[0].message.content)
                ner = validate(payload["text"], payload["spans"])
            except Exception:
                REJECTED += 1
                ner = None
            if ner is None:
                continue
            f.write(
                json.dumps(
                    {
                        "id": f"llm-{s['id']}",
                        "text": payload["text"],
                        "tokenized_text": tokenized_text(payload["text"]),
                        "ner": ner,
                        "provenance": {
                            "source": "llm_synth",
                            "model": model,
                            "base_sample": s["id"],
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            written += 1
    print(f"[llm-rewrite] 入库 {written} 条 -> {out}（校验通过 {VALIDATED} / 拒绝 {REJECTED}）")


if __name__ == "__main__":
    main()
