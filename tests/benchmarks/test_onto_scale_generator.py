"""ontology-scale 生成器单测：确定性（同种子同输出）/ shapes 有效性（平台门禁命中）/计数契约。

规模标定（本机 pyshacl 0.40.1 实测）：10² 档平台校验 <1s，tests 全部用 10²/小档——
10³/10⁴ 档属 smoke/常规跑数据面，不进单测（防门禁膨胀）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from rdflib import Graph

_SIBLING = Path(__file__).resolve().parents[2] / "benchmarks" / "suites" / "ontology-scale"


def _load(module_name: str, file_name: str):
    """同款文件位加载（目录名含连字符不可作包名；runner.py 同款纪律）。"""
    spec = importlib.util.spec_from_file_location(module_name, str(_SIBLING / file_name))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


generator = _load("bench_test_onto_scale_generator", "generator.py")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根（平台校验路径）
from services.ontology.core.shacl import validate  # noqa: E402  # 平台 SHACL 门禁（shacl.py）


class TestGeneratorDeterminism:
    """确定性契约：同种子同输出（逐字节），异种子实例值不同。"""

    def test_同种子双次生成逐字节一致(self):
        # Arrange / Act：同种子连生成两次
        a = generator.generate(30, seed=20261007)
        b = generator.generate(30, seed=20261007)
        # Assert：四图序列化与组装单元逐字节一致
        assert a.tbox_turtle == b.tbox_turtle
        assert a.shapes_turtle == b.shapes_turtle
        assert a.abox_clean.serialize(format="turtle") == b.abox_clean.serialize(format="turtle")
        assert a.abox_violations.serialize(format="turtle") == b.abox_violations.serialize(format="turtle")
        assert a.summary_units == b.summary_units
        assert a.full_units == b.full_units
        assert a.counts == b.counts

    def test_异种子实例值不同而结构同(self):
        # Arrange / Act：换种子
        a = generator.generate(30, seed=1)
        b = generator.generate(30, seed=2)
        # Assert：实例值不同（rng 面），类/形状结构不变（索引派生面）
        assert a.abox_clean.serialize(format="turtle") != b.abox_clean.serialize(format="turtle")
        assert a.tbox_turtle == b.tbox_turtle
        assert a.counts["classes"] == b.counts["classes"]

    def test_档位类数精确等于_scale(self):
        # Act：100 不整除 3（三族分摊 base+余数）
        onto = generator.generate(100)
        # Assert
        assert onto.counts["classes"] == 100
        assert onto.counts["instances_per_form"] == 1000  # instance_multiplier 缺省 10

    def test_非法参数拒绝(self):
        # Act / Assert：scale<3 与 violation_ratio≥1 均拒
        with pytest.raises(ValueError, match="≥3"):
            generator.generate(2)
        with pytest.raises(ValueError, match="violation_ratio"):
            generator.generate(30, violation_ratio=1.0)


class TestShapesValidity:
    """shapes 有效性：平台门禁双形态语义成立 + 违例命中数解析可核对。"""

    def test_干净形态conforms且违例形态命中数恰等(self):
        # Arrange：10² 档（类数=100、实例=1000、违例=10）
        onto = generator.generate(100)
        # Act：平台校验路径（services/ontology/core/shacl.validate，advanced+inference=none）
        clean = validate(onto.abox_clean, onto.shapes, tbox_graph=onto.tbox)
        dirty = validate(onto.abox_violations, onto.shapes, tbox_graph=onto.tbox)
        # Assert：门禁语义（干净过/带违例拒）+ 命中数=生成器解析期望
        assert clean.conforms is True
        assert clean.results == []
        assert dirty.conforms is False
        assert len(dirty.results) == onto.violations_expected == 10

    def test_违例形态覆盖三族约束组件(self):
        # Arrange：违例按族均衡分摊（数值超界/缺必填/枚举外三形态都要出现）
        onto = generator.generate(300, violation_ratio=0.01)  # 3000 实例 ×0.01=30 违例 → 三族各 10
        # Act
        dirty = validate(onto.abox_violations, onto.shapes, tbox_graph=onto.tbox)
        # Assert：三组件各命中 10（sh:maxInclusive / sh:minCount / sh:in），总和=解析期望
        kinds = {}
        for v in dirty.results:
            component = (v.constraint or "").rsplit("#", 1)[-1]
            kinds[component] = kinds.get(component, 0) + 1
        assert kinds == {
            "MaxInclusiveConstraintComponent": 10,
            "MinCountConstraintComponent": 10,
            "InConstraintComponent": 10,
        }
        assert sum(kinds.values()) == onto.violations_expected

    def test_组装文本prolog加全量单元可独立解析(self):
        # Arrange
        onto = generator.generate(30)
        # Act：prolog+full_units 拼接文本 → rdflib 解析
        parsed = Graph()
        parsed.parse(data=onto.full_schema_text(), format="turtle")
        # Assert：可解析且语义自含——每个类公理（owl:Class）与每个 NodeShape targetClass 都在文本内
        from rdflib.namespace import OWL, RDF, SH

        for cls_local in onto.class_props:
            assert (generator.NS[cls_local], RDF.type, OWL.Class) in parsed
            assert (generator.NS[f"Shape_{cls_local}"], SH.targetClass, generator.NS[cls_local]) in parsed

    def test_计数契约与梯度定义一致(self):
        # Act
        onto = generator.generate(60)
        # Assert：类数=档位、属性形状数=2N、实例=10N（梯度「类×属性×实例按比例」的机器核对）
        assert onto.counts["classes"] == 60
        assert onto.counts["properties"] == 2 * 60
        assert onto.counts["instances_per_form"] == 60 * 10
        assert onto.counts["tbox_triples"] == len(onto.tbox)
        assert onto.counts["shapes_triples"] == len(onto.shapes)


class TestMutationSurface:
    """F3 变异面：三契约型保证检出；TBox-only 对照型 inference=none 下盲区（检不出）。"""

    def test_三契约型检出且对照型不检出(self):
        # Arrange
        onto = generator.generate(30)
        targets = generator.mutation_targets(onto)
        assert [t["kind"] for t in targets] == [
            "range_tighten",
            "enum_narrow",
            "required_add",
            "tbox_range_only",
        ]
        # Act / Assert
        for target in targets:
            shapes, tbox, _desc = generator.apply_mutation(onto, target)
            report = validate(onto.abox_clean, shapes, tbox_graph=tbox)
            if target["kind"] == "tbox_range_only":
                assert report.conforms is True, "TBox-only range 改动在 inference=none 下应检不出（已知盲区）"
            else:
                assert report.conforms is False, f"契约型变异 {target['kind']} 必须检出"
                assert len(report.results) > 0

    def test_未知变异类型拒绝(self):
        # Arrange
        onto = generator.generate(30)
        # Act / Assert
        with pytest.raises(ValueError, match="未知变异类型"):
            generator.apply_mutation(onto, {"kind": "no_such_kind", "class_local": "Transformer00000"})
