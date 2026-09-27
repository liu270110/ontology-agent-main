"""mem TBox v1 装载与抽取 schema 生成（06 篇 §3；本体驱动最低验收线）。

制品：services/ontology/turtle/mem.ttl（内建记忆本体，随平台版本发布）。
复用点：core/tbox.load_turtle 装载（解析失败统一 ValueError）；core/shacl.validate 做单条
候选校验（图级入口 + 临时候选图，按位置传 data_graph 规避本环境 pyshacl 双关键字静默不校验
缺陷，inference/advanced 随复用自动对齐）。新增点：抽取 schema 生成（record_type 枚举与类
注释均来自 TBox，替换管线静态 hint）；Observation 仅后台固化，不进抽取 schema。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from services.ontology.core.shacl import validate as core_shacl_validate
from services.ontology.core.tbox import load_turtle

MEM = Namespace("https://ontology-agent.dev/ontology/mem#")  # 与 mem.ttl @prefix mem 一致（06 篇 §3）
_TTL_PATH = Path(__file__).resolve().parent.parent / "turtle" / "mem.ttl"
# 六类可抽取（§3.1 七类去掉 Observation——仅后台固化产生，非实时写入类型）
_EXTRACTABLE = frozenset({"Preference", "FactClaim", "Episode", "Decision", "Goal", "ProcedureRef"})
_CANDIDATE_IRI = "https://ontology-agent.dev/instance/candidate"


@lru_cache(maxsize=1)
def load_mem_graph() -> Graph:
    """mem.ttl → rdflib 内存图（TBox + SHACL shapes 一体；进程内缓存，随平台版本只读）。"""
    graph = load_turtle(_TTL_PATH.read_text(encoding="utf-8"))
    graph.bind("mem", MEM)
    return graph


def generate_extraction_schema() -> dict[str, object]:
    """从 TBox 生成抽取 schema（record_type 枚举与类注释来自 mem.ttl，本体驱动最低验收线）。

    枚举用 `mem:<本地名>` 前缀形式——与 MemoryType（services/memory/domain）取值一致，
    LLM 输出可直接入库；类注释作为抽取提示词素材随 schema 下发。
    """
    graph = load_mem_graph()
    extractable: list[dict[str, str]] = []
    for cls in graph.subjects(RDF.type, OWL.Class):
        local = str(cls).rsplit("#", 1)[-1]
        if local not in _EXTRACTABLE:
            continue
        comment = str(graph.value(cls, RDFS.comment) or "")
        extractable.append({"local": local, "iri": str(cls), "comment": comment})
    extractable.sort(key=lambda e: e["local"])
    return {
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "record_type": {"type": "string", "enum": [f"mem:{e['local']}" for e in extractable]},
                        "subject_iri": {
                            "type": ["string", "null"],
                            "description": "实体 IRI（术语对齐后的唯一 IRI）；不确定给 null，禁止编造",
                        },
                        "content": {"type": "string", "description": "一句话事实正文"},
                        "structured": {
                            "type": "object",
                            "description": '类型化槽位，如 {"attribute": "属性名", "value": "值"}',
                        },
                        "confidence": {"type": "number", "description": "0~1 置信度"},
                    },
                    "required": ["record_type", "content"],
                },
            }
        },
        "_class_comments": {f"mem:{e['local']}": e["comment"] for e in extractable},
    }


def validate_mem_record(payload: dict[str, object]) -> list[str]:
    """pySHACL 单条候选校验（复用 core/shacl 引擎入口）：返回违规信息清单（空=通过）。

    候选 payload → 临时数据图（focus 节点固定为 _CANDIDATE_IRI）→ SHACL shapes 图 = mem.ttl。
    record_type 非法（未知/缺省）直接判违规（fail-closed，门禁语义与 ontology_gate 一致）。
    """
    record_type = str(payload.get("record_type", ""))
    local = record_type.rsplit(":", 1)[-1] if record_type else ""
    if local not in _EXTRACTABLE | {"Observation"} or (MEM[local], RDF.type, OWL.Class) not in load_mem_graph():
        return [f"未知 record_type（不在 mem TBox 七类内）: {record_type or '<缺省>'}"]

    data = Graph()
    data.bind("mem", MEM)
    subject = URIRef(_CANDIDATE_IRI)
    data.add((subject, RDF.type, MEM[local]))
    if payload.get("subject_iri"):
        data.add((subject, MEM.about, URIRef(str(payload["subject_iri"]))))
    if payload.get("content"):
        data.add((subject, MEM.content, Literal(str(payload["content"]))))
    report = core_shacl_validate(data, load_mem_graph(), tbox_graph=load_mem_graph())
    if report.conforms:
        return []
    return [v.message or v.constraint or "SHACL 违规" for v in report.results]
