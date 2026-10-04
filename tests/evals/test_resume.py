# tests/evals/test_resume.py
"""K6-c：evals 断点续跑四件套测试（方案依据 docs/Agent/13 §11）。

- helper 单元（services/evals/_resume.py）：key 稳定性/字段序与跨界/prompt 敏感、
  append 即 load 可见（原子性）、墓碑默认跳过+开关重跑+ok 覆盖、尾行截断容忍、
  fsync 计数（monkeypatch os.fsync）、append_line 往返。
- runner 集成（session_search_schema/runner.py）：假 run_one 跑 mini 任务集，
  零网络/零 git ref 提取——端到端断点续跑幂等、墓碑策略、历史输出引导。
门禁基线：tests/evals 为 K6 新目录（此前 0），完成后 ≥10 用例全绿。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EVALS_DIR = REPO_ROOT / "services" / "evals"
SESSION_DIR = EVALS_DIR / "session_search_schema"
for _p in (EVALS_DIR, SESSION_DIR):  # _resume 与 runner 的平铺导入（from tasks import …）
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _resume  # noqa: E402
import runner as ss_runner  # noqa: E402


# ── K6-a helper 单元 ────────────────────────────────────────────────

def test_content_key_stable_and_hex():
    a = _resume.content_key("t1", "base", "m1", "prompt-1")
    b = _resume.content_key("t1", "base", "m1", "prompt-1")
    assert a == b  # 同输入同 key
    assert len(a) == 64 and a == a.lower()
    int(a, 16)  # sha256 十六进制


def test_content_key_field_order_and_boundaries():
    k = _resume.content_key("t1", "base", "m1", "p")
    # 关键字实参任意顺序 → 同 key（拼接序在函数内固定）
    assert _resume.content_key(
        prompt="p", model="m1", arm="base", task_id="t1") == k
    # 分隔符防跨界碰撞：字段错位不共 key
    assert _resume.content_key("t1x", "base", "m1", "p") != \
        _resume.content_key("t1", "xbase", "m1", "p")


def test_content_key_changes_when_prompt_changes():
    base = _resume.content_key("t1", "base", "m1", "p1")
    assert _resume.content_key("t1", "base", "m1", "p2") != base  # prompt 变 key 变
    assert _resume.content_key("t2", "base", "m1", "p1") != base
    assert _resume.content_key("t1", "cand", "m1", "p1") != base
    assert _resume.content_key("t1", "base", "m2", "p1") != base


def test_append_then_load_visible(tmp_path):
    store = _resume.CheckpointStore(tmp_path / "results")
    assert not store.exists()
    store.append({"key": "k1", "status": "ok"})
    assert store.exists()
    assert store.path.name == ".checkpoint.jsonl"  # 与输出同目录、点前缀隐藏
    done, tomb = store.load()  # append 后立即 load 可见
    assert done == {"k1"} and tomb == {}


def test_tombstone_default_skip_and_retry_flag(tmp_path):
    store = _resume.CheckpointStore(tmp_path / "results")
    store.append({"key": "ok1", "status": "ok"})
    rec = store.tombstone("err1", "RateLimitError: 429")
    assert rec["status"] == "tombstone" and rec["reason"] and rec["ts"]
    done, tomb = store.load()
    assert done == {"ok1"}
    assert tomb["err1"]["reason"] == "RateLimitError: 429" and tomb["err1"]["ts"]
    assert store.is_blocked("err1")  # 墓碑默认跳过
    assert not store.is_blocked("err1", retry_tombstones=True)  # 开关重跑
    assert store.is_blocked("ok1", retry_tombstones=True)  # 已完成一律跳过


def test_ok_overrides_tombstone(tmp_path):
    store = _resume.CheckpointStore(tmp_path / "results")
    store.tombstone("k1", "boom")
    store.append({"key": "k1", "status": "ok"})  # 重跑成功覆盖旧墓碑
    done, tomb = store.load()
    assert done == {"k1"} and tomb == {}  # ok 覆盖旧墓碑：墓碑面清空
    assert store.is_blocked("k1")  # 语义=已完成 → resume 跳过（不再进墓碑重跑面）


def test_truncated_tail_tolerated(tmp_path):
    d = tmp_path / "results"
    d.mkdir()
    (d / ".checkpoint.jsonl").write_text(
        json.dumps({"key": "a", "status": "ok", "ts": "t"}) + "\n"
        + json.dumps({"key": "b", "status": "tombstone",
                      "reason": "r", "ts": "t"}) + "\n"
        + '{"key": "c", "statu',  # 崩溃截断的尾行
        encoding="utf-8")
    store = _resume.CheckpointStore(d)
    with pytest.warns(UserWarning, match="截断"):
        done, tomb = store.load()
    assert done == {"a"} and set(tomb) == {"b"}  # 既有记录不受影响


def test_fsync_called_on_every_persist(tmp_path, monkeypatch):
    calls = []
    real_fsync = os.fsync
    monkeypatch.setattr(
        os, "fsync",
        lambda fd: (calls.append(fd), real_fsync(fd))[1])

    store = _resume.CheckpointStore(tmp_path / "results")
    store.append({"key": "k1", "status": "ok"})
    store.tombstone("k2", "boom")
    with open(tmp_path / "out.jsonl", "a", encoding="utf-8") as f:
        _resume.append_line(f, {"x": 1})
    assert len(calls) == 3  # append / tombstone / append_line 各一次 fsync


def test_append_line_roundtrip(tmp_path):
    p = tmp_path / "out.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        _resume.append_line(f, {"a": 1, "b": "中文"})
        _resume.append_line(f, {"a": 2})
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"a": 1, "b": "中文"}, {"a": 2}]


# ── K6-b runner 集成（假 run_one，mini 任务集，零网络） ─────────────

MINI_TASKS = {
    "t1": ("prompt-1", lambda a: True, ""),
    "t2": ("prompt-2", lambda a: True, ""),
}
MINI_ARMS = {"base": object(), "cand": object()}  # run_one 被替换，arm 不解引用


def _fake_result(task, arm, model):
    return {"task": task, "arm": arm, "model": model, "ok": True,
            "n_tool_calls": 1, "bad_calls": 0, "first_prompt_tokens": 10,
            "total_tokens": 20, "wall_s": 0.1, "calls": [],
            "final": f"final-{task}-{arm}"}


def _battery(tmp_path, monkeypatch, fake, reps=2, **kw):
    monkeypatch.setattr(ss_runner, "run_one", fake)
    monkeypatch.setattr(ss_runner.time, "sleep", lambda s: None)  # 零等待重试
    results = tmp_path / "results"
    outpath = results / "mini-model.jsonl"
    store = _resume.CheckpointStore(results)
    ss_runner.run_battery(client=None, model="mini-model", arms=MINI_ARMS,
                          tasks=MINI_TASKS, outpath=outpath, store=store,
                          main_db=None, reps=reps, **kw)
    return outpath, store


def test_runner_resume_after_interrupt(tmp_path, monkeypatch):
    """跑一半中断（KeyboardInterrupt 不落记录）→ 重跑只补剩余 → 再跑零调用。"""
    calls = {"n": 0}

    def flaky(client, model, arm_name, arm_mod, task_id, prompt, oracle,
              main_db, max_iters=8):
        calls["n"] += 1
        if calls["n"] == 4:  # 第 4 个 cell 执行中被中断
            raise KeyboardInterrupt
        return _fake_result(task_id, arm_name, model)

    with pytest.raises(KeyboardInterrupt):
        _battery(tmp_path, monkeypatch, flaky)
    outpath, store = tmp_path / "results" / "mini-model.jsonl", \
        _resume.CheckpointStore(tmp_path / "results")
    done, tomb = store.load()
    assert len(done) == 3 and not tomb  # 中断前 3 个 cell 已 checkpoint
    assert len(outpath.read_text(encoding="utf-8").splitlines()) == 3

    calls2 = {"n": 0}

    def ok(client, model, arm_name, arm_mod, task_id, prompt, oracle,
           main_db, max_iters=8):
        calls2["n"] += 1
        return _fake_result(task_id, arm_name, model)

    _battery(tmp_path, monkeypatch, ok)  # 续跑：只补剩余 5 个
    rows = [json.loads(x) for x in
            outpath.read_text(encoding="utf-8").splitlines()]
    assert calls2["n"] == 5
    assert len(rows) == 8
    assert {(r["task"], r["arm"], r["rep"]) for r in rows} == {
        (t, a, rep) for t in MINI_TASKS for a in MINI_ARMS for rep in (0, 1)}
    done, tomb = store.load()
    assert len(done) == 8 and not tomb

    _battery(tmp_path, monkeypatch, ok)  # 全 done：零调用、零新行（幂等）
    assert calls2["n"] == 5
    assert len(outpath.read_text(encoding="utf-8").splitlines()) == 8


def test_runner_tombstone_then_retry_flag(tmp_path, monkeypatch):
    """重试耗尽 → 墓碑（输出零行）；resume 默认跳过；--retry-tombstones 重跑。"""

    def failing(client, model, arm_name, arm_mod, task_id, prompt, oracle,
                main_db, max_iters=8):
        raise ValueError("provider down")

    outpath, store = _battery(tmp_path, monkeypatch, failing,
                              max_attempts=2)
    assert not outpath.exists() or \
        not outpath.read_text(encoding="utf-8").splitlines()
    done, tomb = store.load()
    assert not done and len(tomb) == 8  # 2 任务 × 2 rep × 2 arm 全入墓碑
    assert all(v["reason"] == "ValueError: provider down" for v in tomb.values())

    calls = {"n": 0}

    def ok(client, model, arm_name, arm_mod, task_id, prompt, oracle,
           main_db, max_iters=8):
        calls["n"] += 1
        return _fake_result(task_id, arm_name, model)

    _battery(tmp_path, monkeypatch, ok, retry_tombstones=False)
    assert calls["n"] == 0  # 墓碑默认跳过：零调用

    _battery(tmp_path, monkeypatch, ok, retry_tombstones=True)
    assert calls["n"] == 8  # 开关重跑：全部补齐
    rows = [json.loads(x) for x in
            outpath.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 8
    done, tomb = store.load()
    assert len(done) == 8 and not tomb  # 成功后墓碑清空


def test_runner_bootstrap_from_legacy_output(tmp_path, monkeypatch):
    """无 checkpoint 的历史输出目录：旧行回填 done，重跑只补剩余、不重写旧行。"""
    results = tmp_path / "results"
    results.mkdir()
    outpath = results / "mini-model.jsonl"
    legacy = [
        {"task": "t1", "arm": "base", "model": "mini-model", "ok": True,
         "rep": 0},
        {"task": "t1", "arm": "cand", "model": "mini-model", "ok": True,
         "rep": 1},
    ]
    outpath.write_text(
        json.dumps(legacy[0]) + "\n"
        + "not-json\n"  # 历史损坏行：跳过（该 cell 会重跑一次）
        + json.dumps(legacy[1]) + "\n",
        encoding="utf-8")
    store = _resume.CheckpointStore(results)
    assert not store.exists()
    ss_runner._bootstrap_done_from_output(outpath, store, "mini-model",
                                          MINI_TASKS)
    assert store.exists()
    expect = {ss_runner.cell_key("t1", "base", "mini-model", "prompt-1", 0),
              ss_runner.cell_key("t1", "cand", "mini-model", "prompt-1", 1)}
    done, tomb = store.load()
    assert done == expect and not tomb

    calls = {"n": 0}

    def ok(client, model, arm_name, arm_mod, task_id, prompt, oracle,
           main_db, max_iters=8):
        calls["n"] += 1
        return _fake_result(task_id, arm_name, model)

    _battery(tmp_path, monkeypatch, ok)  # 8 cell - 2 已引导 = 6（损坏行 cell 重跑 1 次）
    assert calls["n"] == 6
    lines = outpath.read_text(encoding="utf-8").splitlines()
    assert "not-json" in lines  # append-only：旧行原样保留，不重写
    rows = [json.loads(x) for x in lines if x != "not-json"]
    assert len(rows) == 8  # 2 旧有效行 + 6 新行
