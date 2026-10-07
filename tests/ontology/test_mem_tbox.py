# tests/ontology/test_mem_tbox.py
"""mem TBox v1：Turtle 装载/schema 生成/SHACL 校验（规格 06 篇 §3；本体驱动最低验收线）。"""

import json

import rdflib

from services.ontology.core.mem_tbox import (
    generate_extraction_schema,
    load_mem_graph,
    validate_mem_record,
)


def test_turtle_loads_with_six_extractable_classes():
    g = load_mem_graph()
    classes = {str(c).rsplit("#", 1)[-1] for c in g.subjects(rdflib.RDF.type, rdflib.OWL.Class)}
    # 六类可抽取（Observation 仅后台固化，不在抽取 schema）
    for name in ("Preference", "FactClaim", "Episode", "Decision", "Goal", "ProcedureRef"):
        assert name in classes, name


def test_extraction_schema_has_six_enum_values():
    schema = generate_extraction_schema()
    assert json.dumps(schema)  # 可序列化
    props = schema.get("properties", schema)
    text = json.dumps(props)
    for name in ("mem:Preference", "mem:FactClaim", "mem:Episode", "mem:Decision", "mem:Goal", "mem:ProcedureRef"):
        assert name in text, name
    enum = schema["properties"]["records"]["items"]["properties"]["record_type"]["enum"]
    assert len(enum) == 6  # 枚举恰六类：多或漏都是 TBox 装载/schema 生成缺陷
    assert "mem:Observation" not in text.replace("不进抽取", "")  # Observation 不进抽取枚举


def test_shacl_valid_record_passes():
    violations = validate_mem_record(
        {
            "record_type": "mem:FactClaim",
            "subject_iri": "http://e/s1",
            "content": "A 负责人是张三",
            "confidence": 0.9,
        }
    )
    assert violations == []


def test_shacl_missing_content_fails():
    violations = validate_mem_record(
        {
            "record_type": "mem:FactClaim",
            "subject_iri": "http://e/s1",
            "confidence": 0.9,
        }
    )
    assert violations  # 缺 content 必有违规


def test_shacl_garbage_iri_rejected():
    violations = validate_mem_record(
        {
            "record_type": "mem:FactClaim",
            "subject_iri": "not a uri 张三",  # 空白 + 非 ASCII：良构性门禁必拒（core/tbox.iri_problems）
            "content": "A 负责人是张三",
        }
    )
    assert violations


def test_shacl_non_mem_prefixed_record_type_rejected():
    for bad in ("FactClaim", "pwr:FaultOutage", ""):
        violations = validate_mem_record({"record_type": bad, "content": "x"})
        assert violations, bad  # 非 mem: 前缀（含缺省）直接判违规，不进 SHACL
