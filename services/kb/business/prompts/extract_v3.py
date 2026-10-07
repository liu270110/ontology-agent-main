"""抽取提示词模板 v3（版本化资产；standards/01 §5.1 / 18 篇 §1：模板入库、version pin）。

2026-10-07 K24 批新增（docs/Agent/13 §30 E-3 编号制硬幻觉门禁；上游研究整理/12 mem0@10 §12
UUID→序号反幻觉同型）：v2→v3 变更 = 本体引导目录改编号制 + 规则 2 改序号口径——
- 目录类行只给「序号. 标签（本地名）」，不再暴露全 IRI：模型无从复制/杜撰 IRI；
- ontology_class/object_class 只准回目录序号（整数）；解析侧按声明序建 idx→IRI 映射表映射回
  IRI（kb_extraction._map_class_indices，K24-c），越界/非整数复用 K21 剪枝管线
  OUT_OF_TAXONOMY 留痕（硬幻觉门禁：schema 外产物不直接放行也不静默丢弃）；
- 组装结构（「## 本体引导清单」/「## 抽取文本」两段式）、evidence 逐字引语要求、其余规则正文
  不变；输出 schema 侧 ontology_class/object_class 同步改 integer（kb_extraction
  _EXTRACT_SCHEMA_V3）。
治理约束（18 篇 §1.1）：active 版本正文不可变，修错/调整一律发新版本（extract_v4.py
新文件 + 注册表新登记 + 快照更新走评审回归），禁止原地改写本文件正文。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.ontology.core import tbox

if TYPE_CHECKING:
    from services.kb.business.kb_extraction import SeedCatalog

TEMPLATE_REF = "kb_extract@v3"

# 系统提示词正文（硬要求：禁止凭空创造 / 只回目录序号 / 附原文逐字证据 / 只输出 JSON——
# OntRAG §2.3 表 1 文本知识型要点 + 宪法第 2 条；规则 2 序号口径=编号制门禁的提示词面）。
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


def render_catalog(catalog: SeedCatalog) -> str:
    """本体引导清单区（编号制，K24 §30）：类行=「- {idx}. 类 {label}（{local_name}）」，idx 从 1 起
    按 SeedCatalog.classes 声明序排列——kb_extraction 侧以同序同起点 enumerate 建 idx→IRI 映射
    （声明序=渲染序=映射序不变式，tests/kb 钉死）；属性行保持 v2 口径（本地名，不带 IRI）。
    """
    lines = [
        f"- {idx}. 类 {label}（{local_name}）" for idx, (_, label, local_name) in enumerate(catalog.classes, start=1)
    ]
    lines += [f"- 属性 {tbox.local_name(iri)}（标签：{label}）" for iri, label, _ in catalog.properties]
    return "\n".join(lines)


def render(catalog_text: str, chunk_content: str) -> str:
    """用户提示词组装（结构同 v2）：本体引导清单区 + 空行 + 抽取文本区（FakeModelPort 依「## 抽取文本」标记切分）。"""
    return f"## 本体引导清单\n{catalog_text}\n\n## 抽取文本\n{chunk_content}"
