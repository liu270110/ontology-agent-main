# -*- coding: utf-8 -*-
"""模板正例生成器（离线，无需 LLM）。

依据 GB/T 48000.3 附录 D 实例化风格（标准实体→层次→对象→特性→约束逻辑→行动）
构造带跨度标注的训练句。LLM 改写管线（S1 后期）在模板句基础上做同义改写，
但必须复用本模块产出的 tokenized_text + ner（改写后重新过 char→token 映射）。

输出：data/positives.jsonl，每行
  {"id", "text", "tokenized_text", "ner": [[i, j, label_en], ...],
   "provenance": {"source": "template_v0.1", "template_id", "seed"}}
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from onto_train.tokenizer_utils import (  # noqa: E402
    char_span_to_token_span,
    tokenized_text,
)

ROOT = Path(__file__).resolve().parents[3]

STDS = ["GB/T 31486-2024", "GB/T 48000.3-2026", "GB/T 1.1-2020", "DL/T 634.5104-2025"]
STD_NAMES = ["电动汽车用动力蓄电池", "标准数字化 第3部分：本体建模要求", "标准化工作导则", "远动设备及系统"]
ORGS = ["中国电力企业联合会", "全国汽车标准化技术委员会", "国家标准化管理委员会", "中国电器工业协会"]
CATS = ["电力", "汽车", "电子信息", "有色金属"]
LEVELS = ["第5章", "第6章", "6.2条", "5.3.1条"]
TERMS = ["容量恢复能力", "单体蓄电池", "绝缘电阻", "本体建模"]
OBJS = ["电池包", "连接器", "运动设备", "本体模型"]
CHARS = ["容量特性", "循环寿命", "绝缘性能", "一致性"]
CONSTRAINTS = ["应不超过制造商标称值", "应不低于额定容量", "应满足表2规定"]
ACTIONS = ["进行放电测试", "开展复审", "实施绝缘试验", "提交征求意见稿"]
FORMS = ["表2", "图A.1", "公式(1)", "附录B"]
LAWS = ["《中华人民共和国标准化法》", "《能源法》相关条款"]
STAGES = ["征求意见", "技术审查", "批准发布", "复审"]


class SentenceBuilder:
    """拼接句子并记录每个填入片段的字符跨度。"""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.spans: list[tuple[int, int, str]] = []  # (char_start, char_end, label)

    def lit(self, s: str) -> "SentenceBuilder":
        self.parts.append(s)
        return self

    def ent(self, s: str, label: str) -> "SentenceBuilder":
        start = sum(len(p) for p in self.parts)
        self.parts.append(s)
        self.spans.append((start, start + len(s), label))
        return self

    def build(self) -> tuple[str, list[tuple[int, int, str]]]:
        return "".join(self.parts), list(self.spans)


TEMPLATES = {
    "T01_std_issued": lambda r: SentenceBuilder()
    .ent(r.choice(ORGS), "Stakeholder").lit("发布了").ent(r.choice(STDS), "Standard")
    .lit("《").ent(r.choice(STD_NAMES), "Standard").lit("》。"),
    "T02_clause_term": lambda r: SentenceBuilder()
    .ent(r.choice(STDS), "Standard").lit(r.choice(LEVELS) + "规定了术语")
    .ent(r.choice(TERMS), "Term").lit("。"),
    "T03_object_characteristic": lambda r: SentenceBuilder()
    .lit("本文件适用于").ent(r.choice(OBJS), "Technical Object")
    .lit("，其").ent(r.choice(CHARS), "Characteristic").lit("按").ent(r.choice(FORMS), "Representation Form")
    .lit("执行。"),
    "T04_constraint_action": lambda r: SentenceBuilder()
    .lit("容量恢复能力").ent(r.choice(CONSTRAINTS), "Constraint Logic")
    .lit("，测试时").ent(r.choice(ACTIONS), "Action").lit("。"),
    "T05_stage_law": lambda r: SentenceBuilder()
    .lit("该标准处于").ent(r.choice(STAGES), "Development Stage")
    .lit("阶段，并应符合").ent(r.choice(LAWS), "External Constraint").lit("的要求。"),
    "T06_structure": lambda r: SentenceBuilder()
    .ent(r.choice(STDS), "Standard").lit("的").ent("规范性附录", "Structural Element")
    .lit("给出了").ent(r.choice(TERMS), "Term").lit("的").ent("资料性表述", "Structural Element").lit("。"),
    "T07_cat_level": lambda r: SentenceBuilder()
    .lit("该领域类别为").ent(r.choice(CATS), "Domain Category")
    .lit("，标准层次单元为").ent(r.choice(LEVELS), "Hierarchy Level").lit("。"),
    "T08_object_of_std": lambda r: SentenceBuilder()
    .ent(r.choice(STD_NAMES), "Standard").lit("的标准化对象是")
    .ent(r.choice(OBJS), "Technical Object").lit("。"),
    "T09_term_def": lambda r: SentenceBuilder()
    .ent(r.choice(STDS), "Standard").lit("定义了术语")
    .ent(r.choice(TERMS), "Term").lit("并在").ent(r.choice(LEVELS), "Hierarchy Level").lit("中使用。"),
    "T10_action_chain": lambda r: SentenceBuilder()
    .lit("技术审查通过后，由").ent(r.choice(ORGS), "Stakeholder")
    .ent(r.choice(ACTIONS), "Action").lit("，结果记入").ent(r.choice(FORMS), "Representation Form").lit("。"),
    "T11_dev_stage_full": lambda r: SentenceBuilder()
    .ent(r.choice(STDS), "Standard").lit("经历了征求意见和技术审查，当前处于")
    .ent(r.choice(STAGES), "Development Stage").lit("阶段。"),
    "T12_char_constraint": lambda r: SentenceBuilder()
    .ent(r.choice(OBJS), "Technical Object").lit("的").ent(r.choice(CHARS), "Characteristic")
    .ent(r.choice(CONSTRAINTS), "Constraint Logic").lit("。"),
    "T13_std_chain": lambda r: SentenceBuilder()
    .ent(r.choice(STDS), "Standard").lit("引用并替代旧版标准，其")
    .ent(r.choice(LEVELS), "Hierarchy Level").lit("规定了").ent(r.choice(OBJS), "Technical Object")
    .lit("的").ent(r.choice(CHARS), "Characteristic").lit("。"),
    "T14_multi": lambda r: SentenceBuilder()
    .ent(r.choice(ORGS), "Stakeholder").lit("归口的").ent(r.choice(STDS), "Standard")
    .lit("适用于").ent(r.choice(OBJS), "Technical Object").lit("，要求")
    .ent(r.choice(ACTIONS), "Action").lit("，依据").ent(r.choice(LAWS), "External Constraint").lit("。"),
    "T15_unit_form": lambda r: SentenceBuilder()
    .ent(r.choice(STDS), "Standard").lit("的某").ent("信息单元", "Information Unit")
    .lit("以").ent(r.choice(FORMS), "Representation Form").lit("表述，涉及")
    .ent(r.choice(TERMS), "Term").lit("。"),
    "T16_std_object": lambda r: SentenceBuilder()
    .ent(r.choice(STDS), "Standard").lit("的标准化对象是")
    .ent(r.choice(OBJS), "Standardized Object").lit("，归口单位为")
    .ent(r.choice(ORGS), "Stakeholder").lit("。"),
    "T17_ob2_object_action": lambda r: SentenceBuilder()
    .lit("按 OB2 建模法，").ent(r.choice(OBJS), "OB2 Object")
    .lit("是对象，").ent(r.choice(ACTIONS), "OB2 Action")
    .lit("是改变其状态的行为。"),
    "T18_ob2_event_rule": lambda r: SentenceBuilder()
    .ent(r.choice(["订单支付成功事件", "设备停电上报事件"]), "OB2 Event")
    .lit("满足守卫规则").ent("R" + str(r.randint(1, 99)).zfill(3), "OB2 Rule")
    .lit("的条件下触发。"),
    "T19_ob2_chain": lambda r: SentenceBuilder()
    .ent("状态越限事件", "OB2 Event").lit("触发")
    .ent(r.choice(ACTIONS), "OB2 Action").lit("，结果回写")
    .ent(r.choice(OBJS), "OB2 Object").lit("并记入").ent(r.choice(FORMS), "Representation Form").lit("。"),
}


def generate(count_per_template: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    samples: list[dict] = []
    for tid, factory in sorted(TEMPLATES.items()):
        for k in range(count_per_template):
            text, char_spans = factory(rng).build()
            toks = tokenized_text(text)
            ner = []
            for cs, ce, label in char_spans:
                mapped = char_span_to_token_span(text, cs, ce)
                if mapped is None:
                    raise RuntimeError(f"{tid}: 跨度映射失败 {text[cs:ce]!r}")
                ner.append([mapped[0], mapped[1], label])
            samples.append(
                {
                    "id": f"{tid}-{k:03d}",
                    "text": text,
                    "tokenized_text": toks,
                    "ner": ner,
                    "provenance": {"source": "template_v0.1", "template_id": tid, "seed": seed},
                }
            )
    return samples


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-template", type=int, default=16)
    ap.add_argument("--seed", type=int, default=20260926)
    ap.add_argument("--out", default=str(ROOT / "data" / "positives.jsonl"))
    args = ap.parse_args()

    samples = generate(args.per_template, args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    labels = sorted({lab for s in samples for _, _, lab in s["ner"]})
    print(f"[positives] {len(samples)} 条 -> {out}")
    print(f"[positives] 覆盖标签 {len(labels)} 类: {', '.join(labels)}")


if __name__ == "__main__":
    main()
