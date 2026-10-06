# tests/evals/test_k7_residual.py
"""K7 批 C-7 余量测试（方案依据 docs/Agent/13 §13）。

- K7-a session_search_schema/report.py：(task,arm,rep) keep-first 去重——
  含重复行输入的汇总指标与无重复行输入一致；丢弃计数+UserWarning；
  无 rep 的 legacy 行豁免（合并会污染 n）。
- K7-b readtool/runner.py 收编：逐 cell checkpoint 断点续跑（rep 中途断
  只补剩余 cell）、兼容形态（rep<N>.jsonl 增量 + rep<N>.json 聚合导出 +
  legacy json 引导/合并 keep-first）、report.load_label 兼容读取。
- K7-c core_tool_deferral/orchestrator.py 收编：线程池 4 线程并发写
  checkpoint 完整性（100 条全落）、errored 墓碑语义对齐 K6（默认跳过 +
  --retry-tombstones 重跑）、legacy 结果文件引导（旧 keep 口径）。
- K7-c cell key 与 K6 cell_key 同构（content_key + "::rep<N>" 后缀）。
门禁：tests/evals = K6 基线 12 + 本批 ≥8，全绿零回归。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EVALS_DIR = REPO_ROOT / "services" / "evals"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

import _resume  # noqa: E402


def _load(name: str, path: Path):
    """私有模块名加载 eval 模块：避免与其他 eval harness 的平铺名
    （tasks/fixtures/runner/report）经 sys.modules 串包（K7-b/c 已把
    平铺导入改为惰性/私有名，这里同样防御）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ss_report = _load("k7_ss_report", EVALS_DIR / "session_search_schema" / "report.py")
rt_runner = _load("k7_readtool_runner", EVALS_DIR / "readtool" / "runner.py")
rt_report = _load("k7_readtool_report", EVALS_DIR / "readtool" / "report.py")
orch = _load("k7_deferral_orchestrator", EVALS_DIR / "core_tool_deferral" / "orchestrator.py")


# ── 共享小工厂 ──────────────────────────────────────────────────────


def _row(task, arm, rep, ok, tok, calls):
    """session_search_schema 输出行（runner.append_line 的最小形态）。"""
    return {
        "task": task,
        "arm": arm,
        "rep": rep,
        "ok": ok,
        "n_tool_calls": calls,
        "bad_calls": 0,
        "total_tokens": tok,
        "first_prompt_tokens": 10,
        "wall_s": 0.1,
    }


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _rt_task(tid, prompt):
    return types.SimpleNamespace(task_id=tid, prompt=prompt, capability="c")


def _rt_rec(task_id, score=1.0):
    return {
        "task_id": task_id,
        "capability": "c",
        "score": score,
        "api_turns": 2,
        "tool_calls": 3,
        "read_file_calls": 1,
        "total_tokens": 100,
        "wall_s": 1.0,
        "error": None,
        "final_response": f"final-{task_id}",
    }


RT_TASKS = [_rt_task("t1", "p1"), _rt_task("t2", "p2")]


def _rt_run_battery(fn, out_dir, store, reps, **kw):
    return rt_runner.run_battery(fn, "m1", "nous", "baseline", 1.0, ["file"], RT_TASKS, out_dir, store, reps=reps, **kw)


# ── K7-a：report (task,arm,rep) keep-first 去重 ─────────────────────


def test_report_dedup_keep_first_metrics_match_clean_input(tmp_path):
    """含崩溃窗口重复行的汇总指标与无重复行输入完全一致（keep-first）。"""
    clean = [_row("t1", "base", 0, 1, 100, 3), _row("t1", "cand", 0, 0, 50, 2)]
    dup = list(clean) + [_row("t1", "base", 0, 1, 999, 99)]  # 重复行 token 不同
    f_clean, f_dup = tmp_path / "clean.jsonl", tmp_path / "dup.jsonl"
    _write_jsonl(f_clean, clean)
    _write_jsonl(f_dup, dup)

    grand_clean, dups_clean = ss_report.summarize([f_clean])
    with pytest.warns(UserWarning, match="重复行"):
        grand_dup, dups_dup = ss_report.summarize([f_dup])

    assert dups_clean == 0 and dups_dup == 1  # 后续重复行丢弃并计数
    assert dict(grand_dup) == dict(grand_clean) == {"base": [1, 1, 100, 3], "cand": [0, 1, 50, 2]}  # 999/99 未计入


def test_report_dedup_warns_and_exempts_legacy_rows_without_rep(tmp_path):
    """同 key 后续行丢弃 → UserWarning 带计数；无 rep 的 legacy 行豁免。"""
    dup_rows = [
        _row("t1", "base", 0, 1, 100, 3),
        _row("t1", "base", 0, 1, 100, 3),
        _row("t1", "base", 0, 1, 100, 3),
    ]  # 同 cell 三行
    legacy = [
        {"task": "t1", "arm": "base", "ok": 1, "n_tool_calls": 3, "bad_calls": 0, "total_tokens": 100}
    ] * 2  # 无 rep 字段
    f_dup, f_leg = tmp_path / "dup.jsonl", tmp_path / "legacy.jsonl"
    _write_jsonl(f_dup, dup_rows)
    _write_jsonl(f_leg, legacy)

    with pytest.warns(UserWarning, match="重复行") as rec:
        grand, dups = ss_report.summarize([f_dup, f_leg])
    assert dups == 2  # 后续两行丢弃
    assert len(rec) == 1  # 且仅 dup.jsonl 一条（无 legacy 豁免告警、无杂音）
    assert "dup.jsonl" in str(rec[0].message)
    assert grand["base"] == [3, 3, 300, 9]  # rep 行去重后 1 行 + legacy 2 行


# ── K7-b：readtool 逐 cell 断点续跑 + 兼容形态 ──────────────────────


def test_readtool_resume_mid_rep_only_reruns_remaining(tmp_path):
    """rep 中途断（KeyboardInterrupt）→ 重跑只补剩余 cell → 幂等零调用。"""
    out_dir = tmp_path / "baseline" / "m1"
    store = _resume.CheckpointStore(out_dir)
    calls = {"n": 0}

    def flaky(task, model, provider, timeout_mult, toolsets):
        calls["n"] += 1
        if calls["n"] == 4:  # rep2 的第二个 cell 执行中被中断（rep2 首格已落盘）
            raise KeyboardInterrupt
        return dict(_rt_rec(task.task_id))

    with pytest.raises(KeyboardInterrupt):
        _rt_run_battery(flaky, out_dir, store, reps=2)
    # rep1 完整（2 行 + 聚合导出）；rep2 仅 t1 落盘、聚合未写
    assert len((out_dir / "rep1.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert (out_dir / "rep1.json").exists()
    assert len((out_dir / "rep2.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert not (out_dir / "rep2.json").exists()
    done, tomb = store.load()
    assert len(done) == 3 and not tomb

    calls2 = {"n": 0}

    def ok(task, model, provider, timeout_mult, toolsets):
        calls2["n"] += 1
        return dict(_rt_rec(task.task_id))

    _rt_run_battery(ok, out_dir, store, reps=2)  # 续跑：只补 rep2 的 t2
    assert calls2["n"] == 1
    assert len((out_dir / "rep2.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert (out_dir / "rep2.json").exists()  # 聚合补导出（崩溃窗口自愈）
    recs = {r["task_id"]: r for r in json.loads((out_dir / "rep2.json").read_text(encoding="utf-8"))["records"]}
    assert set(recs) == {"t1", "t2"}
    done, tomb = store.load()
    assert len(done) == 4 and not tomb

    _rt_run_battery(ok, out_dir, store, reps=2)  # 全 done：零调用（幂等）
    assert calls2["n"] == 1
    assert len((out_dir / "rep2.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_readtool_legacy_bootstrap_tombstone_retry_and_report_compat(tmp_path, monkeypatch):
    """无 checkpoint 的历史 rep1.json：good→done、errored→墓碑；墓碑默认
    跳过、开关重跑后 jsonl 新记录覆盖旧 errored 记录；report.load_label
    读重导出的聚合 json 兼容。"""
    results_root = tmp_path / "results"
    out_dir = results_root / "baseline" / "m1"
    out_dir.mkdir(parents=True)
    legacy = {
        "model": "m1",
        "provider": "nous",
        "label": "baseline",
        "rep": 1,
        "records": [_rt_rec("t1", 1.0), dict(_rt_rec("t2", 0.0), error="TimeoutError: boom")],
    }
    (out_dir / "rep1.json").write_text(json.dumps(legacy), encoding="utf-8")
    tasks_by_id = {t.task_id: t for t in RT_TASKS}
    store = _resume.CheckpointStore(out_dir)

    rt_runner._bootstrap_done_from_legacy(out_dir, store, "baseline", "m1", tasks_by_id)
    done, tomb = store.load()
    assert done == {rt_runner.cell_key("t1", "baseline", "m1", "p1", 1)}
    assert set(tomb) == {rt_runner.cell_key("t2", "baseline", "m1", "p2", 1)}
    assert tomb[rt_runner.cell_key("t2", "baseline", "m1", "p2", 1)]["reason"] == "TimeoutError: boom"

    calls = {"n": 0}

    def ok(task, model, provider, timeout_mult, toolsets):
        calls["n"] += 1
        return dict(_rt_rec(task.task_id, 0.9))

    _rt_run_battery(ok, out_dir, store, reps=1)  # 墓碑默认跳过：零调用
    assert calls["n"] == 0
    assert not (out_dir / "rep1.jsonl").exists()

    _rt_run_battery(ok, out_dir, store, reps=1, retry_tombstones=True)  # 开关重跑 t2
    assert calls["n"] == 1
    recs = {r["task_id"]: r for r in json.loads((out_dir / "rep1.json").read_text(encoding="utf-8"))["records"]}
    assert recs["t2"]["score"] == 0.9 and recs["t2"]["error"] is None
    assert recs["t1"]["score"] == 1.0  # 旧 good 记录保留
    done, tomb = store.load()
    assert len(done) == 2 and not tomb  # 重跑成功覆盖旧墓碑

    monkeypatch.setattr(rt_report, "RESULTS", results_root)
    loaded = rt_report.load_label("baseline", None)
    assert loaded["m1"]["t2"]["score"] == [0.9]  # 兼容面读到重跑结果
    assert loaded["m1"]["t1"]["score"] == [1.0]


def test_readtool_crash_window_selfheal_and_jsonl_keep_first(tmp_path):
    """崩溃窗口（行已 append、checkpoint 缺 key 不发生本例——本例为
    checkpoint 齐、聚合 json 未写）续跑零重跑并自愈补导出；jsonl 内部
    重复行聚合时 keep-first。"""
    out_dir = tmp_path / "baseline" / "m1"
    out_dir.mkdir(parents=True)
    rows = [
        dict(_rt_rec("t1"), total_tokens=111),
        dict(_rt_rec("t2")),
        dict(_rt_rec("t1"), total_tokens=999),
    ]  # 崩溃窗口重复行
    with open(out_dir / "rep1.jsonl", "a", encoding="utf-8") as f:
        for r in rows:
            _resume.append_line(f, r)
    store = _resume.CheckpointStore(out_dir)
    store.append({"key": rt_runner.cell_key("t1", "baseline", "m1", "p1", 1), "status": "ok"})
    store.append({"key": rt_runner.cell_key("t2", "baseline", "m1", "p2", 1), "status": "ok"})

    calls = {"n": 0}

    def must_not_run(task, model, provider, timeout_mult, toolsets):
        calls["n"] += 1
        return dict(_rt_rec(task.task_id))

    _rt_run_battery(must_not_run, out_dir, store, reps=1)
    assert calls["n"] == 0  # checkpoint 齐全：零重跑
    data = json.loads((out_dir / "rep1.json").read_text(encoding="utf-8"))
    assert len(data["records"]) == 2  # keep-first：t1 重复行只保留首行
    t1 = next(r for r in data["records"] if r["task_id"] == "t1")
    assert t1["total_tokens"] == 111  # 首行（首次结果）胜出


# ── K7-c：core_tool_deferral CheckpointStore 收编 ───────────────────


def _deferral_tasks(n):
    return {f"s{i}": {"id": f"s{i}", "prompt": f"p{i}", "timeout": 5} for i in range(n)}


def test_deferral_concurrent_checkpoint_completeness_4t_100(tmp_path, monkeypatch):
    """线程池 4 线程并发写 checkpoint：100 条全落、无交错损坏、全量跳过。"""
    results = tmp_path / "results"
    results.mkdir()
    tasks_by_id = _deferral_tasks(10)
    store = _resume.CheckpointStore(results)
    lock = threading.Lock()
    cells = orch.plan_cells(
        store, str(results), "org/m1", [f"s{i}" for i in range(10)], ["base", "pr"], 5, tasks_by_id=tasks_by_id
    )
    assert len(cells) == 100  # 10 任务 × 2 arm × 5 rep

    def fake_run_cell(cell, model, py):
        time.sleep(0.001)  # 制造线程交错
        return (cell, "OK", "")

    monkeypatch.setattr(orch, "run_cell", fake_run_cell)
    orch.run_battery(cells, store, lock, "org/m1", "unused-py", parallel=4)

    lines = (results / ".checkpoint.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100  # 100 条全落
    assert all(json.loads(x)["status"] == "ok" for x in lines)  # 无交错损坏
    done, tomb = store.load()
    assert len(done) == 100 and not tomb
    assert (
        orch.plan_cells(
            store, str(results), "org/m1", [f"s{i}" for i in range(10)], ["base", "pr"], 5, tasks_by_id=tasks_by_id
        )
        == []
    )  # 全部跳过


def test_deferral_tombstone_semantics_k6_aligned(tmp_path, monkeypatch):
    """errored（WORKER_ERR/TIMEOUT）→ 墓碑：默认跳过、开关重跑；
    INFRA_ABORT 不落记录（对齐旧"无文件重跑"）。"""
    results = tmp_path / "results"
    results.mkdir()
    tasks_by_id = _deferral_tasks(2)  # s0/s1；本测试只用 s1
    store = _resume.CheckpointStore(results)
    lock = threading.Lock()
    cells = orch.plan_cells(store, str(results), "m1", ["s1"], ["base", "pr"], 2, tasks_by_id=tasks_by_id)
    assert len(cells) == 4

    def fake(cell, model, py):
        if cell[0] == "base":
            return (cell, "WORKER_ERR", "worker exit 1")
        if cell[2] == 1:
            return (cell, "INFRA_ABORT", "ABORT: no tree")
        return (cell, "TIMEOUT", "")

    monkeypatch.setattr(orch, "run_cell", fake)
    orch.run_battery(cells, store, lock, "m1", "py", parallel=4)
    done, tomb = store.load()
    assert not done
    kb = orch.cell_key("base", "s1", "m1", "p1", 1)
    kp2 = orch.cell_key("pr", "s1", "m1", "p1", 2)
    assert set(tomb) == {kb, orch.cell_key("base", "s1", "m1", "p1", 2), kp2}
    assert tomb[kb]["reason"] == "worker exit 1"
    assert tomb[kp2]["reason"] == "wall timeout"
    assert orch.cell_key("pr", "s1", "m1", "p1", 1) not in done | set(tomb)

    plan = orch.plan_cells(store, str(results), "m1", ["s1"], ["base", "pr"], 2, tasks_by_id=tasks_by_id)
    assert [(c[0], c[2]) for c in plan] == [("pr", 1)]  # 墓碑默认跳过
    plan_retry = orch.plan_cells(
        store, str(results), "m1", ["s1"], ["base", "pr"], 2, retry_tombstones=True, tasks_by_id=tasks_by_id
    )
    assert len(plan_retry) == 4  # --retry-tombstones：全部放行重跑


def test_deferral_bootstrap_from_legacy_results(tmp_path):
    """历史结果文件引导（对齐旧 keep 口径）：good/attempted→done、
    errored→墓碑、损坏文件→重跑、transcript/未知 task→忽略。"""
    results = tmp_path / "results"
    results.mkdir()

    def w(name, obj):
        (results / name).write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")

    w("base__s1__rep1.json", {"error": None, "score": 1.0})  # good → done
    w("pr__s1__rep1.json", {"error": "boom", "score": 0.0})  # errored → 墓碑
    w("base__s2__rep1.json", {"error": "x", "score": 0.5})  # attempted → done
    w("pr__s2__rep1.json", "{corrupt")  # 损坏 → 重跑
    w("base__s1__rep1.json.transcript.json", {"error": None})  # 忽略
    tasks_by_id = {"s1": {"id": "s1", "prompt": "p1"}, "s2": {"id": "s2", "prompt": "p2"}}
    store = _resume.CheckpointStore(results)

    orch.bootstrap_from_results(str(results), store, "m1", tasks_by_id)
    done, tomb = store.load()
    assert done == {orch.cell_key("base", "s1", "m1", "p1", 1), orch.cell_key("base", "s2", "m1", "p2", 1)}
    assert set(tomb) == {orch.cell_key("pr", "s1", "m1", "p1", 1)}
    assert tomb[orch.cell_key("pr", "s1", "m1", "p1", 1)]["reason"] == "boom"

    cells = orch.plan_cells(store, str(results), "m1", ["s1", "s2"], ["base", "pr"], 1, tasks_by_id=tasks_by_id)
    assert [(c[0], c[1], c[2]) for c in cells] == [("pr", "s2", 1)]  # 只剩损坏


def test_deferral_cell_key_k6_pattern():
    """cell key 与 K6 同构：content_key 前缀 + "::rep<N>" 后缀、字段敏感。"""
    k = orch.cell_key("base", "s1", "m1", "p1", 1)
    assert k.endswith("::rep1")
    assert k == orch.cell_key("base", "s1", "m1", "p1", 1)  # 同输入同 key
    assert k.split("::")[0] == _resume.content_key("s1", "base", "m1", "p1")
    assert orch.cell_key("pr", "s1", "m1", "p1", 1) != k
    assert orch.cell_key("base", "s1", "m1", "p2", 1) != k  # prompt 参与哈希
    assert orch.cell_key("base", "s1", "m1", "p1", 2) != k  # rep 后缀区分
