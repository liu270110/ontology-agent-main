"""抽取提示词模板 v4（版本化资产；standards/01 §5.1 / 18 篇 §1：模板入库、version pin）。

v3→v4 变更（2026-10-07 合流裁决：本批冻结期实验版（原 ce543e8 以 ``kb_extract@v3`` 登记）
与 develop 已落地的 K24 ``kb_extract@v3`` 编号制门禁同 ref 不同义——注册表同键不可并存，按
治理约束「active 版本正文不可变，变更一律发新版本」将本实验版重登记为 v4，双方语义复合保留）：

- 目录与序号口径沿用 v3（K24 §30 编号目录制硬幻觉门禁原样生效）：本体引导清单仍由
  ``extract_v3.render_catalog`` 渲染（kb_extraction 三表按 ref 选取），类序号→IRI 解析侧映射
  （_map_class_indices）启用面不变；输出 schema 的 ontology_class/object_class 维持 integer；
- 新增「标题栏结构化线索」区（:func:`render` 新增可选 ``titleblock_fields`` 参数，缺省 None =
  与 v3 输出逐字节一致，非图纸文档零行为变化）：确定性投影产物（仅作位置线索）+ 扩展标题栏
  字段词表（GB/T 10609.1 标题栏组成区，中英别名）+ 输出 JSON schema 强约束；
- 系统提示词正文与 v3 逐字节一致（加固只动用户提示词组装面，不动系统规则面）。

实测依据（services/devtools/drawing-probe/extract_lab.py，真机 vLLM qwen3-4b-awq 三轮复现，
golden 字段级口径复用 eval_golden 对齐函数；脚本零真实图号、样图路径经环境变量注入）：
- baseline（实验时现网 v2）：字段命中 TP=0（三轮：候选 23/6/6 条，命中字段均为空）；
- 线索区版（同 hint 正文）：TP=3 三轮稳定（图号/材料/表面处理），R 0→0.375，
  F1 0→0.353/0.316/0.316（宏 F1≈0.33）；few-shot / 生成参数 / prompt 级思考开关
  各对照组均 TP=0（实验数据见原实验提交附言与 extract_lab 输出）。

治理约束（18 篇 §1.1）：active 版本正文不可变，修错/调整一律发新版本（extract_v5.py
新文件 + 注册表新登记 + 快照更新走评审回归），禁止原地改写本文件正文。
"""

from __future__ import annotations

from collections.abc import Mapping

TEMPLATE_REF = "kb_extract@v4"

# 系统提示词正文（与 v3 逐字节一致；v4 只动用户提示词组装面——标题栏结构化线索区；
# 规则 2 序号口径=编号制门禁的提示词面，随 v3 原样生效）。
SYSTEM_PROMPT = """你是电力配电网领域的知识抽取引擎。从「抽取文本」中抽取实体/属性/关系/事件候选。
规则（违反即无效）：
1. 禁止凭空创造：只抽取文本明确提及的内容，每条候选必须能在原文中找到依据；
2. ontology_class 与 object_class 只能填「本体引导清单」类条目的序号（整数，如 2）——清单
   未展示类 IRI，禁止自行构造；清单没有合适类时省略该字段；
3. predicate（如有）优先取清单中的属性本地名（如 hasStatus/orderNo）；
4. confidence ∈ [0,1]，反映该候选的确定性；
5. evidence 必须是「抽取文本」中的原文逐字片段（禁止改写、概括、拼接，每条候选附一条）；
   出处四元组（source_ref）由系统自动附加，禁止生成，候选之间不得互为证据；
6. 只输出 JSON 对象：{"candidates": [{"kind", "name", "ontology_class", "predicate",
   "object", "evidence", "confidence", "detail", "properties"}]}，kind ∈ entity|relation|attribute|event，
   relation/attribute 必须附 predicate 与 object，properties 为「属性本地名 → 字符串值」；
7. 文本没有任何可抽取内容时返回 {"candidates": []}。"""

# 扩展标题栏字段词表（GB/T 10609.1 标题栏组成区：代号/名称区、材料标记区、签字区、其他区；
# 中英别名对齐真实图纸标题栏英文标签——词汇来源为制图国标，非任何评测资产；
# 09 篇 v2 承接项 extract_lab 实测：九字段窄词表 TP=0，本扩展词表 TP=3，词表覆盖是关键变量）。
FIELD_VOCAB: tuple[str, ...] = (
    "图号(图样代号/Drawing NO/DWG NO)",
    "名称(图名/Title)",
    "材料(Material)",
    "表面处理(Finish/Surface Treatment)",
    "热处理(Heat Treat)",
    "数量(Qty/Pcs)",
    "比例(Scale)",
    "重量(Weight)",
    "幅面(Size/Sheet Size)",
    "版本(Rev/Version)",
    "日期(Date)",
    "设计(Design/Drawn)",
    "审核(Check/Checked)",
    "批准(Approval/Approved)",
    "共几张(Sheet,如 1 OF 4 形态)",
    "项目名(Project Name)",
)

# 输出 JSON schema 的提示词内嵌字面量（冻结字符串；与 kb_extraction._EXTRACT_SCHEMA_V3 一致——
# v4 沿用 K24 integer 序号口径，仅本字面量的 ontology_class/object_class 同步 integer；
# 一致性由 tests/kb 快照断言锁定——prompts 不反向 import kb_extraction，防循环依赖）。
# extract_lab 实测：schema 强约束入提示词是 hint 生效三要素之一。
OUTPUT_SCHEMA_JSON: str = (
    '{"type": "object", "additionalProperties": false, "required": ["candidates"], '
    '"properties": {"candidates": {"type": "array", "maxItems": 64, "items": '
    '{"type": "object", "additionalProperties": false, "required": ["kind", "name", "confidence"], '
    '"properties": {"kind": {"enum": ["entity", "relation", "attribute", "event"]}, '
    '"name": {"type": "string", "minLength": 1, "maxLength": 256}, '
    '"ontology_class": {"type": "integer", "minimum": 1}, '
    '"predicate": {"type": "string", "maxLength": 256}, '
    '"object": {"type": "string", "maxLength": 1024}, '
    '"object_class": {"type": "integer", "minimum": 1}, '
    '"evidence": {"type": "string", "maxLength": 2048}, '
    '"confidence": {"type": "number", "minimum": 0, "maximum": 1}, '
    '"detail": {"type": "string", "maxLength": 2048}, '
    '"properties": {"type": "object", "additionalProperties": {"type": "string"}}}}}}}'
)


def _hint_section(titleblock_fields: Mapping[str, str]) -> str:
    """标题栏结构化线索区（extract_lab A2 实测正文，逐字节与实测提示词一致）。"""
    field_lines = "\n".join(f"{name}: {value}" for name, value in titleblock_fields.items())
    vocab_lines = "\n".join(f"- {name}" for name in FIELD_VOCAB)
    return (
        "## 标题栏结构化线索（确定性投影产物，可能含错配——仅作位置线索，取值须以「抽取文本」原文为准）\n"
        f"{field_lines if field_lines else '（投影零命中：标题栏字段未配对，需你从乱序文本中按标签语义配对）'}\n\n"
        "## 候选字段清单（标题栏字段；输出 attribute 候选时 predicate 取清单中的**字段中文名**（括号外部分），"
        '每个可配对字段一条：{"kind": "attribute", "name": "<字段值所属实体>", '
        '"predicate": "<字段名>", "object": "<字段值>"}）\n'
        f"{vocab_lines}\n\n"
        "## 输出 JSON schema（强约束，违反即无效）\n"
        f"{OUTPUT_SCHEMA_JSON}\n"
    )


def render(catalog_text: str, chunk_content: str, titleblock_fields: Mapping[str, str] | None = None) -> str:
    """用户提示词组装：本体引导清单区（v3 编号目录正文，kb_extraction 按 ref 渲染传入）+
    （图纸文档）标题栏结构化线索区 + 空行 + 抽取文本区。

    ``titleblock_fields=None``（缺省）：与 v3 输出逐字节一致（非图纸/未投影文档零行为变化；
    FakeModelPort 依「## 抽取文本」标记切分的契约不变——标记恒在末段正文前）。
    传 dict（含空表）时插入标题栏结构化线索区（kb_extraction.run_extract 以文档
    meta.titleblock 传入；extract_lab 实测该区使 qwen3-4b 在乱序图纸文本上的 golden
    字段命中 0→3）。值一律来自运行期文档 meta，本模块零写死字段值（宪法第 2 条）。
    """
    catalog_area = f"## 本体引导清单\n{catalog_text}\n\n"
    hint = "" if titleblock_fields is None else _hint_section(titleblock_fields)
    return f"{catalog_area}{hint}## 抽取文本\n{chunk_content}"
