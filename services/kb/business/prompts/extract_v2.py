"""抽取提示词模板 v2（版本化资产；standards/01 §5.1 / 18 篇 §1：模板入库、version pin）。

2026-09-28 深化批次合流：正文自 kb_extraction 的 ``_EXTRACT_PROMPT_V2`` 原位平移，逐字节未变。
v1→v2 变更 = 新增 evidence 原文逐字引语要求（规则 5 重写 + 规则 6 schema 增 evidence 字段）——
与 kb_extraction 证据逐字门禁（evidence_not_in_chunk）配套。
治理约束（18 篇 §1.1）：active 版本正文不可变，修错/调整一律发新版本（extract_v3.py
新文件 + 注册表新登记 + 快照更新走评审回归），禁止原地改写本文件正文。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.ontology.core import tbox

if TYPE_CHECKING:
    from services.kb.business.kb_extraction import SeedCatalog

TEMPLATE_REF = "kb_extract@v2"

# 系统提示词正文（硬要求：禁止凭空创造 / 附原文逐字证据 / 只输出 JSON——OntRAG §2.3 表 1
# 文本知识型要点 + 宪法第 2 条）。
SYSTEM_PROMPT = """你是电力配电网领域的知识抽取引擎。从「抽取文本」中抽取实体/属性/关系/事件候选。
规则（违反即无效）：
1. 禁止凭空创造：只抽取文本明确提及的内容，每条候选必须能在原文中找到依据；
2. ontology_class 只能取自「本体引导清单」中的类 IRI；清单没有合适类时省略该字段；
3. predicate（如有）优先取清单中的属性本地名（如 hasStatus/orderNo）；
4. confidence ∈ [0,1]，反映该候选的确定性；
5. evidence 必须是「抽取文本」中的原文逐字片段（禁止改写、概括、拼接，每条候选附一条）；
   出处四元组（source_ref）由系统自动附加，禁止生成，候选之间不得互为证据；
6. 只输出 JSON 对象：{"candidates": [{"kind", "name", "ontology_class", "predicate",
   "object", "evidence", "confidence", "detail", "properties"}]}，kind ∈ entity|relation|attribute|event，
   relation/attribute 必须附 predicate 与 object，properties 为「属性本地名 → 字符串值」；
7. 文本没有任何可抽取内容时返回 {"candidates": []}。"""


def render_catalog(catalog: SeedCatalog) -> str:
    """本体引导清单区（本体引导 = 种子类/属性清单注入提示词，OntRAG §2.3）：声明序，属性取本地名。"""
    lines = [f"- 类 {iri}（标签：{label}）" for iri, label, _ in catalog.classes]
    lines += [f"- 属性 {tbox.local_name(iri)}（标签：{label}）" for iri, label, _ in catalog.properties]
    return "\n".join(lines)


def render(catalog_text: str, chunk_content: str) -> str:
    """用户提示词组装：本体引导清单区 + 空行 + 抽取文本区（FakeModelPort 依「## 抽取文本」标记切分）。"""
    return f"## 本体引导清单\n{catalog_text}\n\n## 抽取文本\n{chunk_content}"
