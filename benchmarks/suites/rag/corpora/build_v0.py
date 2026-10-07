"""rag v0 金标语料生成器（确定性模板，零随机）：50 文档 × 20 查询。

产出（corpora/v0/，文件一并版本化入库，可由本脚本再生校验）：
- documents.jsonl：每行 {"doc_id","title","content","doc_type"}；
- queries.jsonl：每行 {"query_id","query","relevant":[{"doc_id","quote","note"}]}——
  「文档-查询-相关片段」三元组（docs/Agent/16 §1）。

设计约束：
- 领域=电力停电分析（wedge 场景，AGENTS.md 设计宪法 4）；事实表=facts.json（数据文件，
  可审可再生），文档分四型：停电工单 ×20 / 设备档案 ×15 / 检修记录 ×10 / 调控规程 ×5；
- 引用片段（quote）由渲染文档的同一模板函数拼出——**引文与正文同源**，loader 加载时
  断言 quote ∈ content（漂移即失败，防金标失真）；
- 查询覆盖 11 单相关 / 6 双相关 / 3 三相关文档，跨四文档型均有分布；
- 零随机：重跑本脚本逐字节稳定（sha256 可作语料指纹）。

用法（仓库根）：python benchmarks/suites/rag/corpora/build_v0.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CORPORA_DIR = Path(__file__).resolve().parent / "v0"
_FACTS_PATH = CORPORA_DIR / "facts.json"

# ---------------------------------------------------------------- 引文构造器（与渲染同源 → 引文必在正文）


def wo_passage(w: dict[str, Any]) -> str:
    """故障经过句（工单正文与查询引文共用本函数拼串）。"""
    return (
        f"{w['out_time']}，{w['sub']}{w['line']}因{w['cause']}发生{w['fault']}，"
        f"出线开关跳闸，{w['area']}部分用户停电，影响{w['users']}户。"
    )


def wo_measure_sentence(w: dict[str, Any]) -> str:
    return f"抢修班已赴现场{w['measure']}，当前处置状态：{w['status']}。"


def eq_rating_line(e: dict[str, Any]) -> str:
    return f"- 额定参数：{e['rating']}"


def eq_vendor_line(e: dict[str, Any]) -> str:
    return f"- 生产厂商：{e['vendor']}"


def eq_model_line(e: dict[str, Any]) -> str:
    return f"- 型号：{e['model']}"


def eq_patrol_sentence(e: dict[str, Any]) -> str:
    return f"{e['patrol_date']}巡视，结论：{e['patrol_conclusion']}。"


def mx_part_sentence(m: dict[str, Any]) -> str:
    return f"本次检修更换部件：{m['part']}。"


def rg_clause_sentence(c: dict[str, Any]) -> str:
    return f"第{c['num']}条（{c['title']}）：{c['text']}"


# ---------------------------------------------------------------- 文档渲染


def _render_work_order(w: dict[str, Any]) -> tuple[str, str]:
    title = f"停电工单 {w['no']}（{w['line']}）"
    content = (
        f"# {title}\n\n"
        f"- 线路：{w['line']}\n"
        f"- 变电站：{w['sub']}\n"
        f"- 区域：{w['area']}\n"
        f"- 故障类型：{w['fault']}\n"
        f"- 停电时间：{w['out_time']}\n"
        f"- 影响用户：{w['users']} 户\n"
        f"- 处置状态：{w['status']}\n\n"
        f"## 故障经过\n{wo_passage(w)}\n\n"
        f"## 处置措施\n{wo_measure_sentence(w)}\n\n"
        f"## 备注\n本工单由{w['sub']}值班调度（{w['dispatcher']}）生成，"
        f"处置过程遵循《配网故障处置调控规程》相关条款。\n"
    )
    return title, content


def _render_equipment(e: dict[str, Any]) -> tuple[str, str]:
    title = f"设备档案 {e['no']}（{e['sub']}{e['name']}）"
    content = (
        f"# {title}\n\n"
        f"- 设备编号：{e['no']}\n"
        f"- 设备名称：{e['name']}\n"
        f"- 所属变电站：{e['sub']}\n"
        f"- 设备类型：{e['etype']}\n"
        f"{eq_model_line(e)}\n"
        f"{eq_vendor_line(e)}\n"
        f"- 投运日期：{e['commission']}\n"
        f"{eq_rating_line(e)}\n\n"
        f"## 最近巡视\n{eq_patrol_sentence(e)}\n"
    )
    return title, content


def _render_maintenance(m: dict[str, Any]) -> tuple[str, str]:
    title = f"检修记录 {m['no']}（{m['target']}）"
    related = m.get("related_wo") or "无"
    content = (
        f"# {title}\n\n"
        f"- 检修单号：{m['no']}\n"
        f"- 检修对象：{m['target']}\n"
        f"- 检修日期：{m['date']}\n"
        f"- 工作负责人：{m['leader']}\n"
        f"- 关联工单：{related}\n\n"
        f"## 工作内容\n{m['date']}对{m['target']}开展{m['work']}。\n\n"
        f"## 更换部件\n{mx_part_sentence(m)}\n\n"
        f"## 验收结论\n验收结论：{m['conclusion']}。\n"
    )
    return title, content


def _render_regulation(r: dict[str, Any]) -> tuple[str, str]:
    title = f"{r['name']}（{r['no']}）"
    clauses = "\n\n".join(rg_clause_sentence(c) for c in r["clauses"])
    content = f"# {title}\n\n## 条款摘编\n\n{clauses}\n"
    return title, content


# ---------------------------------------------------------------- 金标装配


def load_facts() -> dict[str, Any]:
    """事实表（数据文件）：结构校验后原样返回。"""
    facts = json.loads(_FACTS_PATH.read_text(encoding="utf-8"))
    expected = {"work_orders": 20, "equipments": 15, "maintenances": 10, "regulations": 5}
    for key, count in expected.items():
        if len(facts.get(key, [])) != count:
            raise ValueError(f"facts.json {key} 应为 {count} 条，实际 {len(facts.get(key, []))}")
    return facts


def build_documents() -> list[dict[str, Any]]:
    """渲染 50 文档（20 工单 + 15 设备 + 10 检修 + 5 规程），id 稳定前缀编码。"""
    facts = load_facts()
    docs: list[dict[str, Any]] = []
    for i, w in enumerate(facts["work_orders"], start=1):
        title, content = _render_work_order(w)
        docs.append({"doc_id": f"wo-{i:03d}", "title": title, "content": content, "doc_type": "work_order"})
    for i, e in enumerate(facts["equipments"], start=1):
        title, content = _render_equipment(e)
        docs.append({"doc_id": f"eq-{i:03d}", "title": title, "content": content, "doc_type": "equipment"})
    for i, m in enumerate(facts["maintenances"], start=1):
        title, content = _render_maintenance(m)
        docs.append({"doc_id": f"mx-{i:03d}", "title": title, "content": content, "doc_type": "maintenance"})
    for i, r in enumerate(facts["regulations"], start=1):
        title, content = _render_regulation(r)
        docs.append({"doc_id": f"rg-{i:03d}", "title": title, "content": content, "doc_type": "regulation"})
    return docs


def _ref(doc_key: str, quote: str, note: str) -> dict[str, str]:
    """查询金标引用项：doc_key 形如 "wo:GD-2601-001"（事实表编号寻址），quote 与正文同源。"""
    prefix, _, no = doc_key.partition(":")
    facts = load_facts()
    index_map = {
        "wo": [w["no"] for w in facts["work_orders"]],
        "eq": [e["no"] for e in facts["equipments"]],
        "mx": [m["no"] for m in facts["maintenances"]],
        "rg": [r["no"] for r in facts["regulations"]],
    }
    idx = index_map[prefix].index(no) + 1
    return {"doc_id": f"{prefix}-{idx:03d}", "quote": quote, "note": note}


def _by_no(table: str, no: str) -> dict[str, Any]:
    return next(row for row in load_facts()[table] if row["no"] == no)


def _clause(rg_no: str, num: str) -> dict[str, Any]:
    rg = _by_no("regulations", rg_no)
    return next(c for c in rg["clauses"] if c["num"] == num)


def build_queries() -> list[dict[str, Any]]:
    """20 查询金标：11 单相关 / 6 双相关 / 3 三相关，覆盖四文档型。"""
    wo1 = _by_no("work_orders", "GD-2601-001")
    wo2 = _by_no("work_orders", "GD-2601-002")
    wo3 = _by_no("work_orders", "GD-2601-003")
    wo4 = _by_no("work_orders", "GD-2601-004")
    wo5 = _by_no("work_orders", "GD-2601-005")
    wo6 = _by_no("work_orders", "GD-2601-006")
    wo9 = _by_no("work_orders", "GD-2601-009")
    wo10 = _by_no("work_orders", "GD-2601-010")
    wo11 = _by_no("work_orders", "GD-2601-011")
    wo15 = _by_no("work_orders", "GD-2601-015")
    wo17 = _by_no("work_orders", "GD-2601-017")
    mx1 = _by_no("maintenances", "JX-2603-001")
    mx4 = _by_no("maintenances", "JX-2603-004")
    mx5 = _by_no("maintenances", "JX-2603-005")
    mx6 = _by_no("maintenances", "JX-2604-006")
    eq2 = _by_no("equipments", "SB-1002")
    eq5 = _by_no("equipments", "SB-1005")
    eq7 = _by_no("equipments", "SB-2002")
    eq11 = _by_no("equipments", "SB-3002")
    eq13 = _by_no("equipments", "SB-4001")
    return [
        {
            "query_id": "q-001",
            "query": "滨海甲线这次跳闸是什么原因引起的",
            "relevant": [_ref("wo:GD-2601-001", wo_passage(wo1), "故障经过句（雷击闪络）")],
        },
        {
            "query_id": "q-002",
            "query": "东港变电站 2 号主变的额定容量是多少",
            "relevant": [_ref("eq:SB-1002", eq_rating_line(eq2), "额定参数行")],
        },
        {
            "query_id": "q-003",
            "query": "翠竹路这次停电影响了多少用户",
            "relevant": [_ref("wo:GD-2601-004", wo_passage(wo4), "故障经过句含影响户数")],
        },
        {
            "query_id": "q-004",
            "query": "金湾线设备故障的抢修结果怎么样",
            "relevant": [
                _ref("wo:GD-2601-003", wo_measure_sentence(wo3), "处置措施句"),
                _ref("mx:JX-2603-001", mx_part_sentence(mx1), "更换部件句（关联工单）"),
            ],
        },
        {
            "query_id": "q-005",
            "query": "配网故障处置规程对复电顺序是怎么规定的",
            "relevant": [_ref("rg:GC-001", rg_clause_sentence(_clause("GC-001", "4.2")), "复电顺序条款")],
        },
        {
            "query_id": "q-006",
            "query": "有哪些停电是树木倒伏压线引起的",
            "relevant": [
                _ref("wo:GD-2601-002", wo_passage(wo2), "树障工单 1 故障经过句"),
                _ref("wo:GD-2601-006", wo_passage(wo6), "树障工单 2 故障经过句"),
                _ref("wo:GD-2601-010", wo_passage(wo10), "树障工单 3 故障经过句"),
            ],
        },
        {
            "query_id": "q-007",
            "query": "锦绣变电站新投运的开关是什么型号",
            "relevant": [_ref("eq:SB-2002", eq_model_line(eq7), "型号行（2024 投运）")],
        },
        {
            "query_id": "q-008",
            "query": "施工挖断电缆造成的外力破坏停电有哪些",
            "relevant": [
                _ref("wo:GD-2601-004", wo_passage(wo4), "外破工单 1 故障经过句"),
                _ref("wo:GD-2601-017", wo_passage(wo17), "外破工单 2 故障经过句"),
            ],
        },
        {
            "query_id": "q-009",
            "query": "JX-2603-005 这次检修更换了什么部件",
            "relevant": [_ref("mx:JX-2603-005", mx_part_sentence(mx5), "更换部件句")],
        },
        {
            "query_id": "q-010",
            "query": "白鹭洲片区这次鸟害跳闸影响了多少用户",
            "relevant": [_ref("wo:GD-2601-005", wo_passage(wo5), "故障经过句（鸟害相间短路）")],
        },
        {
            "query_id": "q-011",
            "query": "梅岭 1 号电容器最近巡视发现了什么问题",
            "relevant": [_ref("eq:SB-4001", eq_patrol_sentence(eq13), "巡视结论句（熔丝熔断）")],
        },
        {
            "query_id": "q-012",
            "query": "雷雨季节绝缘子闪络导致的跳闸有哪些",
            "relevant": [
                _ref("wo:GD-2601-001", wo_passage(wo1), "雷击工单 1 故障经过句"),
                _ref("wo:GD-2601-009", wo_passage(wo9), "雷击工单 2 故障经过句"),
                _ref("wo:GD-2601-015", wo_passage(wo15), "雷击工单 3 故障经过句"),
            ],
        },
        {
            "query_id": "q-013",
            "query": "停电作业前验电和装设接地线有什么要求",
            "relevant": [_ref("rg:GC-002", rg_clause_sentence(_clause("GC-002", "3.4")), "验电接地条款")],
        },
        {
            "query_id": "q-014",
            "query": "危急缺陷的处理时限是多久",
            "relevant": [_ref("rg:GC-003", rg_clause_sentence(_clause("GC-003", "2.6")), "处理时限条款")],
        },
        {
            "query_id": "q-015",
            "query": "青枫线树障清理后更换了哪些部件",
            "relevant": [
                _ref("mx:JX-2603-004", mx_part_sentence(mx4), "更换部件句"),
                _ref("wo:GD-2601-010", wo_measure_sentence(wo10), "处置措施句（关联工单）"),
            ],
        },
        {
            "query_id": "q-016",
            "query": "龙山线这条架空线路最近巡视结论怎么样",
            "relevant": [_ref("eq:SB-3002", eq_patrol_sentence(eq11), "巡视结论句")],
        },
        {
            "query_id": "q-017",
            "query": "用户设备越级跳闸的问题该怎么防范",
            "relevant": [
                _ref("rg:GC-005", rg_clause_sentence(_clause("GC-005", "6.1")), "定值整定条款"),
                _ref("wo:GD-2601-011", wo_passage(wo11), "越级跳闸工单故障经过句"),
            ],
        },
        {
            "query_id": "q-018",
            "query": "望海线停电后更换了什么设备、现在什么状态",
            "relevant": [
                _ref("mx:JX-2604-006", mx_part_sentence(mx6), "更换部件句（避雷器）"),
                _ref("wo:GD-2601-009", wo_measure_sentence(wo9), "处置措施句"),
            ],
        },
        {
            "query_id": "q-019",
            "query": "雷雨季重合闸的投退有什么规定",
            "relevant": [_ref("rg:GC-004", rg_clause_sentence(_clause("GC-004", "5.7")), "重合闸投退条款")],
        },
        {
            "query_id": "q-020",
            "query": "东湾新城 1 号主变是哪个厂商生产的",
            "relevant": [_ref("eq:SB-1005", eq_vendor_line(eq5), "生产厂商行")],
        },
    ]


# ---------------------------------------------------------------- 校验与落盘


def validate(documents: list[dict], queries: list[dict]) -> None:
    """金标自洽校验：数量、id 唯一、引文逐字在正文（三元组契约核心）。"""
    if len(documents) != 50:
        raise ValueError(f"文档数应为 50，实际 {len(documents)}")
    if len(queries) != 20:
        raise ValueError(f"查询数应为 20，实际 {len(queries)}")
    ids = [d["doc_id"] for d in documents]
    if len(set(ids)) != len(ids):
        raise ValueError("doc_id 存在重复")
    contents = {d["doc_id"]: d["content"] for d in documents}
    qids = [q["query_id"] for q in queries]
    if len(set(qids)) != len(qids):
        raise ValueError("query_id 存在重复")
    for q in queries:
        if not q["relevant"]:
            raise ValueError(f"{q['query_id']} 无相关文档")
        for ref in q["relevant"]:
            content = contents.get(ref["doc_id"])
            if content is None:
                raise ValueError(f"{q['query_id']} 引用了不存在的 {ref['doc_id']}")
            if ref["quote"] not in content:
                raise ValueError(f"{q['query_id']} 引文不在 {ref['doc_id']} 正文（金标漂移）：{ref['quote'][:40]}…")


def corpus_fingerprint(documents: list[dict], queries: list[dict]) -> str:
    """语料指纹：两集合规范化字节的 sha256（环境指纹随结果落盘，ORSI 场景集哈希同源）。"""
    payload = json.dumps(
        {"documents": documents, "queries": queries}, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    documents = build_documents()
    queries = build_queries()
    validate(documents, queries)
    CORPORA_DIR.mkdir(parents=True, exist_ok=True)
    for name, rows in (("documents.jsonl", documents), ("queries.jsonl", queries)):
        path = CORPORA_DIR / name
        with path.open("w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        print(f"写入 {path}（{len(rows)} 行）")
    print(f"语料指纹 sha256: {corpus_fingerprint(documents, queries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
