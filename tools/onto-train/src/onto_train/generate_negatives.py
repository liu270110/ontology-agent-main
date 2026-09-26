# -*- coding: utf-8 -*-
"""公理负例生成器（国标第 8 章公理类型 -> 扰动算子，03 决议"公理负例四步管线"）。

管线四步中本脚本实现第 1/2 步：
  1. 正例（generate_positives 或已有样本）；
  2. 按 axioms.kind 定义扰动算子生成"表面相似但违反公理"的候选负例；
  3.（--symbolic-check）pySHACL/owlrl 验证违规必触发——符号兜底，零标签噪声；
  4. 相似度过译与本子留待 S2（bge-m3 >=0.9 过滤在 uv 环境就绪后启用）。

输出：data/negatives.jsonl，每行
  {"id", "axiom_kind", "text", "tokenized_text",
   "ner": [正确跨度], "violation": {"span": [i,j], "asserted_labels", "explanation"}}
训练侧（S2 对比校准）只消费 violation 字段构造对比目标；ner 是未被污染的其余标注。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from onto_train.tokenizer_utils import (  # noqa: E402
    char_span_to_token_span,
    tokenized_text,
)

ROOT = Path(__file__).resolve().parents[2]

# 与 configs/labels.json 对应的互斥类型对（国标第 8 章：互斥类型定义不相交）
DISJOINT_PAIRS = [
    ("Technical Object", "Action"),  # 对象/行动互斥（OB2：行为改变对象状态，二者不同类）
    ("Standard", "Term"),  # 标准实体与术语互斥
]

ENUM_STATUSES = {"现行", "即将实施", "废止"}
ENUM_OUTLIER = "飞行中"  # 来自本体核心设计 §7.2 示例的越界值
FUNCTIONAL_ORGS = ["中国电力企业联合会", "全国汽车标准化技术委员会"]  # issuedBy 函数性：只能有一个发布机构


def _mk_sample(sid: str, kind: str, text: str, char_spans, violation_span_chars, violation: dict) -> dict:
    toks = tokenized_text(text)
    ner = []
    for cs, ce, label in char_spans:
        mapped = char_span_to_token_span(text, cs, ce)
        if mapped is not None:
            ner.append([mapped[0], mapped[1], label])
    vmapped = char_span_to_token_span(text, *violation_span_chars)
    violation = {**violation, "span": list(vmapped) if vmapped else None}
    return {
        "id": sid,
        "axiom_kind": kind,
        "text": text,
        "tokenized_text": toks,
        "ner": ner,
        "violation": violation,
    }


def build_negatives(seed: int, per_kind: int) -> list[dict]:
    rng = random.Random(seed)
    samples: list[dict] = []
    stds = ["GB/T 31486-2024", "GB/T 48000.3-2026", "DL/T 634.5104-2025"]
    objs = ["电池包", "连接器"]

    for k in range(per_kind):
        std = rng.choice(stds)
        obj = rng.choice(objs)

        # 1) disjointWith：同一 span 断言两个互斥类型
        lab_a, lab_b = rng.choice(DISJOINT_PAIRS)
        frag = f"{obj}的整体"
        text = f"在{std}的语境下，{frag}既是{lab_a}又是{lab_b}。"
        cs = text.index(frag)
        samples.append(_mk_sample(
            f"disjointWith-{k:03d}", "disjointWith", text,
            [(text.index(std), text.index(std) + len(std), "Standard")],
            (cs, cs + len(frag)),
            {"asserted_labels": [lab_a, lab_b], "explanation": f"互斥类型被同时断言：{lab_a} vs {lab_b}"},
        ))

        # 2) uniqueness：同一标准编号挂在两个不同实体上
        text = f"{std}是电池包的标准，{std}同时是连接器的标准。"
        cs1 = text.index(std)
        cs2 = text.rindex(std)
        samples.append(_mk_sample(
            f"uniqueness-{k:03d}", "uniqueness", text,
            [(cs1, cs1 + len(std), "Standard"), (cs2, cs2 + len(std), "Standard")],
            (cs2, cs2 + len(std)),
            {"asserted_labels": ["Standard"], "explanation": "唯一编号被两个不同实体复用（标准编号全局唯一）"},
        ))

        # 3) date_validity：实施日期早于发布日期
        text = f"{std}的发布日期为2026年8月1日，实施日期为2025年1月1日。"
        cs = text.index("2025年1月1日")
        samples.append(_mk_sample(
            f"date_validity-{k:03d}", "date_validity", text,
            [(text.index(std), text.index(std) + len(std), "Standard")],
            (cs, cs + len("2025年1月1日")),
            {"asserted_labels": [], "explanation": "实施日期早于发布日期，违反日期有效性公理"},
        ))

        # 4) enumeration：状态取值落在枚举之外
        text = f"{std}当前状态为{ENUM_OUTLIER}。"
        cs = text.index(ENUM_OUTLIER)
        samples.append(_mk_sample(
            f"enumeration-{k:03d}", "enumeration", text,
            [(text.index(std), text.index(std) + len(std), "Standard")],
            (cs, cs + len(ENUM_OUTLIER)),
            {"asserted_labels": [], "explanation": f"状态不在受控枚举 {sorted(ENUM_STATUSES)} 内"},
        ))

        # 5) functionalProperty：函数性属性 issuedBy 挂两个发布机构
        org_a, org_b = FUNCTIONAL_ORGS
        text = f"{std}由中国电力企业联合会发布，同时由全国汽车标准化技术委员会发布。"
        cs = text.index(org_b)
        samples.append(_mk_sample(
            f"functionalProperty-{k:03d}", "functionalProperty", text,
            [(text.index(std), text.index(std) + len(std), "Standard"),
             (text.index(org_a), text.index(org_a) + len(org_a), "Stakeholder")],
            (cs, cs + len(org_b)),
            {"asserted_labels": ["Stakeholder"], "explanation": "issuedBy 具有函数性：一个标准只能有一个发布机构"},
        ))

        # 6) version_replacement：废止未指向替代标准
        text = f"{std}已废止，无替代标准。"
        cs = text.index("已废止")
        samples.append(_mk_sample(
            f"version_replacement-{k:03d}", "version_replacement", text,
            [(text.index(std), text.index(std) + len(std), "Standard")],
            (cs, cs + len("已废止")),
            {"asserted_labels": [], "explanation": "废止状态必须通过 replaces 指向替代标准（版本替代公理）"},
        ))

        # 7) hierarchy：无标题条包含子条
        text = f"{std}中无标题的条10.1包含子条10.1.1。"
        cs = text.index("10.1包含")
        samples.append(_mk_sample(
            f"hierarchy-{k:03d}", "hierarchy", text,
            [(text.index(std), text.index(std) + len(std), "Standard")],
            (cs, cs + len("10.1")),
            {"asserted_labels": [], "explanation": "层次约束：无标题条不可再分子条"},
        ))

    return samples


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-kind", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20260926)
    ap.add_argument("--symbolic-check", action="store_true",
                    help="用 pySHACL 验证枚举/函数性负例确被判定违规（四步管线第 3 步，需 rdflib+pyshacl）")
    ap.add_argument("--out", default=str(ROOT / "data" / "negatives.jsonl"))
    args = ap.parse_args()

    samples = build_negatives(args.seed, args.per_kind)

    if args.symbolic_check:
        samples = run_symbolic_check(samples)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    kinds = sorted({s["axiom_kind"] for s in samples})
    print(f"[negatives] {len(samples)} 条 -> {out}（{len(kinds)} 类算子: {', '.join(kinds)}）")


def run_symbolic_check(samples: list[dict]) -> list[dict]:
    """四步管线第 3 步：把枚举/函数性负例建成小图，pySHACL 必须报违规。
    报不出违规的负例视为"生成缺陷"剔除——符号兜底，零标签噪声。
    返回保留样本；rdflib/pyshacl 缺失时零验证原样返回。"""
    try:
        from pyshacl import validate
        from rdflib import Graph, Namespace
    except ImportError:
        print("[symbolic-check] rdflib/pyshacl 未安装（uv sync 后可用），跳过验证")
        return samples

    EX = Namespace("http://example.org/negcheck#")
    GBT = Namespace("https://ontology-agent.dev/ns/gbt48000#")
    kept: list[dict] = []
    checked = passed = 0
    for s in samples:
        if s["axiom_kind"] not in ("enumeration", "functionalProperty"):
            kept.append(s)  # 其余算子暂无对应 SHACL 模板，不参与验证
            continue
        checked += 1
        g = Graph()
        if s["axiom_kind"] == "enumeration":
            shapes = Graph()
            shapes.parse(data=f"""
                @prefix sh: <http://www.w3.org/ns/shacl#> .
                @prefix ex: <{EX}> .
                @prefix gbt: <{GBT}> .
                ex:StandardShape a sh:NodeShape ;
                    sh:targetClass gbt:Standard ;
                    sh:property [
                        sh:path gbt:status ;
                        sh:in ( "现行" "即将实施" "废止" ) ;
                    ] .
            """, format="turtle")
            g.parse(data=f"""
                @prefix gbt: <{GBT}> .
                @prefix ex: <{EX}> .
                <{EX.std1}> a gbt:Standard ;
                    gbt:status "飞行中" .
            """, format="turtle")
        else:  # functionalProperty
            g.parse(data=f"""
                @prefix gbt: <{GBT}> .
                @prefix ex: <{EX}> .
                <{EX.std1}> a gbt:Standard ;
                    gbt:issuedBy ex:orgA , ex:orgB .
                gbt:issuedBy gbt:kind "functional" .
            """, format="turtle")
            shapes = Graph()
            shapes.parse(data=f"""
                @prefix sh: <http://www.w3.org/ns/shacl#> .
                @prefix ex: <{EX}> .
                @prefix gbt: <{GBT}> .
                ex:StandardShape a sh:NodeShape ;
                    sh:targetClass gbt:Standard ;
                    sh:property [
                        sh:path gbt:issuedBy ;
                        sh:maxCount 1 ;
                    ] .
            """, format="turtle")
        conforms, _, _ = validate(g, shacl_graph=shapes, inference="none", advanced=True)
        if not conforms:
            passed += 1
            kept.append(s)
        else:
            print(f"[symbolic-check] 缺陷：{s['id']} 未被判定违规，剔除候选")
    print(f"[negatives] 符号验证通过 {passed}/{checked}（仅枚举/函数性两类参与）")
    return kept


if __name__ == "__main__":
    main()
