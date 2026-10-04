"""golden 评测脚本单测（v1.5 wedge；假 golden + 假候选桩，零 HTTP 零真实图号）。

脚本本体 services/devtools/drawing-probe/eval_golden.py（services/devtools 目录非包，经 importlib 按路径加载）。
不要求本批跑出真实 F1 数值（golden 由验收人另出，路径经 GOLDEN_PATH 注入）——本文件
只锁评测口径：字段级命中判定 / P/R/F1 计算 / 宏平均与 not_found 排除 / golden 骨架校验。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "services" / "devtools" / "drawing-probe" / "eval_golden.py"

_spec = importlib.util.spec_from_file_location("eval_golden_under_test", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
eval_golden = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("eval_golden_under_test", eval_golden)
_spec.loader.exec_module(eval_golden)  # 模块自举 sys.path（脚本内 parents[3]=仓库根），services.* 可导入


# ---------------------------------------------------------------- field_hit（字段级命中口径）


def test_field_hit_命中口径():
    candidate = {"subject": "SAMPLE-0009", "predicate": "图号", "object": "SAMPLE-0009"}
    assert eval_golden.field_hit(candidate, "图号", "SAMPLE-0009")  # object 精确 + 字段名被引用
    assert eval_golden.field_hit(candidate, "图号", " SAMPLE-0009 ")  # 期望值去空白口径
    assert eval_golden.field_hit(
        {"subject": "图号", "predicate": None, "object": "SAMPLE-0009"}, "图号", "SAMPLE-0009"
    )  # predicate 可缺（subject 引用字段名即算）
    assert not eval_golden.field_hit(
        {"subject": "SAMPLE-0009", "predicate": None, "object": "SAMPLE-0009"}, "图号", "SAMPLE-0009"
    )  # subject 只有值、无字段名引用 → 不命中（对齐口径从严）
    assert not eval_golden.field_hit(candidate, "材料", "SAMPLE-0009")  # 字段名未被引用 → 不命中
    assert not eval_golden.field_hit(candidate, "图号", "SAMPLE-0001")  # 值不精确相等 → 不命中
    assert not eval_golden.field_hit(
        {"subject": "SAMPLE-0009", "predicate": "图号", "object": None}, "图号", "SAMPLE-0009"
    )  # 空对象候选 → 不命中


# ---------------------------------------------------------------- doc_metrics（P/R/F1 计算）


def test_doc_metrics_理想全中():
    expected = {"图号": "SAMPLE-0001", "材料": "Q235-B"}
    candidates = [
        {"subject": "SAMPLE-0001", "predicate": "图号", "object": "SAMPLE-0001"},
        {"subject": "安装板", "predicate": "材料", "object": "Q235-B"},
    ]
    assert eval_golden.doc_metrics(expected, candidates) == {
        "tp": 2,
        "fp": 0,
        "fn": 0,
        "precision": 1.0,
        "recall": 1.0,
        "f1": 1.0,
    }


def test_doc_metrics_错值计FP_重复计FP_漏报计FN():
    expected = {"图号": "SAMPLE-0001", "材料": "Q235-B", "比例": "1:50"}
    candidates = [
        {"subject": "SAMPLE-0001", "predicate": "图号", "object": "SAMPLE-0001"},  # TP 图号
        {"subject": "安装板", "predicate": "材料", "object": "AL6061"},  # FP：引用材料字段但值错
        {"subject": "安装板", "predicate": "材料", "object": "Q235-B"},  # TP 材料
        {"subject": "SAMPLE-0001", "predicate": "图号", "object": "SAMPLE-0001"},  # FP：图号重复命中
        {"subject": "泵", "predicate": "suppliedBy", "object": "变电站S1"},  # 无关候选：不进字段级口径
    ]
    m = eval_golden.doc_metrics(expected, candidates)
    assert (m["tp"], m["fp"], m["fn"]) == (2, 2, 1)
    assert m["precision"] == pytest.approx(2 / 4)  # TP/(TP+FP)
    assert m["recall"] == pytest.approx(2 / 3)  # TP/(TP+FN)
    assert m["f1"] == pytest.approx(2 * (2 / 4) * (2 / 3) / ((2 / 4) + (2 / 3)))


def test_doc_metrics_空期望_全零不被除零炸():
    m = eval_golden.doc_metrics({}, [{"subject": "x", "predicate": "图号", "object": "x"}])
    assert m["tp"] == 0 and m["precision"] == 0.0 and m["recall"] == 0.0 and m["f1"] == 0.0
    assert eval_golden.doc_metrics({}, [])["f1"] == 0.0


def test_doc_metrics_全漏报():
    m = eval_golden.doc_metrics({"图号": "SAMPLE-0002"}, [])
    assert (m["tp"], m["fp"], m["fn"]) == (0, 0, 1)
    assert m["precision"] == 0.0 and m["recall"] == 0.0


# ---------------------------------------------------------------- evaluate（宏平均 + not_found 排除）


def test_evaluate_假golden_假候选桩():
    golden = {
        "documents": [
            {  # 全中文档
                "file": "SAMPLE-a.pdf",
                "titleblock": {"图号": "SAMPLE-0001", "材料": "Q235-B"},
            },
            {  # 平台未解析到 → not_found（不计均值）
                "file": "SAMPLE-missing.pdf",
                "titleblock": {"图号": "SAMPLE-0002"},
            },
            {  # 找到但 golden 空 → ok 但不计均值
                "file": "SAMPLE-empty.pdf",
                "titleblock": {},
            },
        ]
    }
    store = {
        "SAMPLE-a.pdf": [
            {"subject": "SAMPLE-0001", "predicate": "图号", "object": "SAMPLE-0001"},
            {"subject": "板", "predicate": "材料", "object": "Q235-B"},
        ],
        "SAMPLE-empty.pdf": [],
    }

    def fake_fetch(entry: dict) -> list[dict] | None:
        return store.get(entry["file"])  # missing 文件 → None = not_found 桩

    report = eval_golden.evaluate(golden, fake_fetch)
    assert [d["file"] for d in report["documents"]] == ["SAMPLE-a.pdf", "SAMPLE-missing.pdf", "SAMPLE-empty.pdf"]
    assert report["documents"][1]["status"] == "not_found"
    assert report["documents"][2]["status"] == "ok" and report["documents"][2]["f1"] == 0.0
    assert report["scored_documents"] == 1  # 只有全中文档计入均值（empty-golden/not_found 排除）
    assert report["not_found"] == 1
    assert report["mean"]["precision"] == pytest.approx(1.0)
    assert report["mean"]["recall"] == pytest.approx(1.0)
    assert report["mean"]["f1"] == pytest.approx(1.0)


def test_evaluate_空golden_均值零():
    report = eval_golden.evaluate({"documents": []}, lambda _entry: None)
    assert report["documents"] == [] and report["scored_documents"] == 0
    assert report["mean"] == {"precision": 0.0, "recall": 0.0, "f1": 0.0}


# ---------------------------------------------------------------- load_golden（骨架校验）


def test_load_golden_合法骨架(tmp_path):
    good = tmp_path / "golden.json"
    payload = {"documents": [{"file": "SAMPLE-a.pdf", "titleblock": {"图号": "SAMPLE-0001"}}]}
    good.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    data = eval_golden.load_golden(good)
    assert data["documents"][0]["titleblock"]["图号"] == "SAMPLE-0001"


def test_load_golden_缺documents列表_ValueError(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="documents"):
        eval_golden.load_golden(bad)


def test_load_golden_条目缺titleblock_ValueError(tmp_path):
    bad = tmp_path / "bad2.json"
    bad.write_text(json.dumps({"documents": [{"file": "SAMPLE-a.pdf"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="第 0 条"):
        eval_golden.load_golden(bad)


# ---------------------------------------------------------------- resolve_document_id（信封兼容防御）


class _FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


class _FakeClient:
    def __init__(self, body: dict) -> None:
        self.body = body
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url: str, params: dict | None = None) -> _FakeResponse:
        self.calls.append((url, params))
        return _FakeResponse(self.body)


def test_resolve_document_id_信封list形态_精确命中():
    """data=list 形态（B1 信封现状）：title 精确匹配优先。"""
    client = _FakeClient({"data": [{"id": "doc-1", "name": "SAMPLE-a"}, {"id": "doc-2", "name": "SAMPLE-ab"}]})
    assert eval_golden.resolve_document_id(client, "http://x", "SAMPLE-a.pdf") == "doc-1"  # 精确先于包含


def test_resolve_document_id_信封items形态_包含命中():
    """data={items:[...]} 形态（真机诊断并存形态）：同口径解析，不绑死单一信封。"""
    client = _FakeClient({"data": {"items": [{"id": "doc-9", "name": "前缀 SAMPLE-b 后缀"}]}})
    assert eval_golden.resolve_document_id(client, "http://x", "SAMPLE-b.pdf") == "doc-9"  # 包含匹配次之


def test_resolve_document_id_两形态空数据_未解析返回None():
    """data 缺失/list 空/{items:[]} 均 → None（not_found 口径，不抛不炸）。"""
    for body in ({}, {"data": []}, {"data": {}}, {"data": {"items": []}}, {"data": {"other": 1}}):
        assert eval_golden.resolve_document_id(_FakeClient(body), "http://x", "SAMPLE-c.pdf") is None


def test_print_report_not_found_带诊断提示(capsys):
    """not_found 明细须附「租户隔离 + title=文件名 stem」诊断行（2026-10-05 真机排查约定）。"""
    report = {
        "documents": [{"file": "SAMPLE-missing.pdf", "status": "not_found", "expected_fields": 2}],
        "mean": {"precision": 0.0, "recall": 0.0, "f1": 0.0},
        "scored_documents": 0,
        "not_found": 1,
    }
    eval_golden.print_report(report)
    out = capsys.readouterr().out
    assert "[not_found] SAMPLE-missing.pdf" in out
    assert "PROBE_TENANT_ID" in out and "stem" in out  # 两条诊断要点齐备
