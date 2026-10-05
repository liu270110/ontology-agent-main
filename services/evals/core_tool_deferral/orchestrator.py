#!/usr/bin/env python3
"""Orchestrate the PR #97979 A/B battery. Resume-safe; per-run wall timeout.

Usage: orchestrator.py <model_slug> <reps> [--tasks=id1,id2] [--arms=base,pr]
                       [--parallel=N] [--retry-tombstones]
Results land in results/<model_short>/<arm>__<task>__rep<r>.json (override
the results root with ABDEFER_RESULTS).

Resume (K7-c, docs/Agent/13 §13; helper=../_resume.py): cell completion is
tracked in results/<model_short>/.checkpoint.jsonl (content-addressed key =
content_key(task, arm, model, prompt) + "::rep<r>"), replacing the old
"result-file exists" skip + delete-errored-and-retry. Errored cells
(WORKER_ERR / TIMEOUT — they still write an errored record json) are
tombstoned: skipped on resume, re-run with --retry-tombstones (K6
semantics). INFRA_ABORT cells leave no record and stay unmarked — they
re-run on the next resume (old behavior: no file → rerun). Historic result
dirs without a checkpoint bootstrap it on first run: good/attempted
records (error empty or score>0, the old keep rule) → done, errored
records → tombstone, unparseable files → left for re-run.

Threading: worker cells run in a ThreadPoolExecutor; checkpoint writes from
worker threads are serialized by a threading.Lock (in-process thread
safety). Single-writer constraint (K6 P2②): a checkpoint file must not be
written by more than one PROCESS at a time — the lock covers threads only;
never run two orchestrators against the same results dir concurrently.
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HARNESS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HARNESS))  # 共享 helper：services/evals/_resume.py

from _resume import CheckpointStore, content_key  # noqa: E402

_taskmod = None


def _load_taskmod():
    """惰性加载本目录 tasks.py（私有模块名）。

    不用平铺名 `import tasks`：其他 eval 模块（session_search_schema /
    readtool）在同名下有不同内容，同进程共存（pytest 收集）时会经
    sys.modules 串包拿错模块。
    """
    global _taskmod
    if _taskmod is None:
        spec = importlib.util.spec_from_file_location(
            "core_tool_deferral_tasks", os.path.join(HARNESS, "tasks.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["core_tool_deferral_tasks"] = mod
        spec.loader.exec_module(mod)
        _taskmod = mod
    return _taskmod


def cell_key(arm, task_id, model, prompt, rep):
    """内容寻址 cell key：content_key(task, arm, model, prompt) + rep 后缀。

    与 session_search_schema/runner.cell_key 同构（docs/Agent/13 §11/§13）：
    rep 是显式重复维度，不进哈希、以固定后缀拼接；prompt 一变 key 即变。
    """
    return f"{content_key(task_id, arm, model, prompt)}::rep{rep}"


def bootstrap_from_results(results_dir, store, model, tasks_by_id):
    """升级引导：结果目录尚无 checkpoint 时，从既有 <arm>__<task>__rep<r>.json
    回填（对齐旧跳过口径）。

    good/attempted 记录（error 为空或 score>0，旧 keep 规则）→ 物化 done；
    errored 记录 → 墓碑（resume 默认跳过，--retry-tombstones 重跑）；
    损坏文件 / 文件名不合模式 / task 已不在电池 → 跳过不回填，对应 cell
    重跑（worker 会覆盖写 json）。
    """
    for name in sorted(os.listdir(results_dir)):
        if not name.endswith(".json") or name.endswith(".transcript.json"):
            continue
        parts = name[:-len(".json")].split("__")
        if len(parts) < 3 or not parts[-1].startswith("rep"):
            continue
        arm, task_id, rep = parts[0], "__".join(parts[1:-1]), parts[-1][3:]
        task = tasks_by_id.get(task_id)
        if not rep.isdigit() or task is None:
            continue
        path = os.path.join(results_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue  # 损坏结果文件：该 cell 重跑
        key = cell_key(arm, task_id, model, task["prompt"], int(rep))
        if rec.get("error") is not None and rec.get("score", 0) <= 0:
            store.tombstone(key, str(rec.get("error")))
        else:
            store.append({"key": key, "status": "ok", "src": "bootstrap"})


def plan_cells(store, results_dir, model, task_ids, arms, reps,
               retry_tombstones=False, tasks_by_id=None):
    """checkpoint 驱动的待跑 cell 计划（替代旧"结果文件存在跳过+删错重跑"）。

    已完成 cell 一律跳过；墓碑 cell 默认跳过，retry_tombstones=True 放行
    （K6 语义对齐）。未知 task 跳过不排。
    """
    tasks_by_id = tasks_by_id if tasks_by_id is not None \
        else _load_taskmod().TASKS_BY_ID
    store.load()
    cells = []
    for task_id in task_ids:
        task = tasks_by_id.get(task_id)
        if task is None:
            continue
        for arm in arms:
            for rep in range(1, reps + 1):
                key = cell_key(arm, task_id, model, task["prompt"], rep)
                if store.is_blocked(key, retry_tombstones=retry_tombstones):
                    continue
                out = os.path.join(
                    results_dir, f"{arm}__{task_id}__rep{rep}.json")
                cells.append((arm, task_id, rep, out, key))
    return cells


def run_cell(cell, model, py):
    """跑单个 cell（子进程 worker，独立结果 json）；返回 (cell, status, info)。"""
    arm, task_id, rep, out, _key = cell
    timeout = _load_taskmod().TASKS_BY_ID[task_id].get("timeout", 600)
    cmd = [py, os.path.join(HARNESS, "worker.py"), arm, model, task_id,
           str(rep), out]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout + 60, env=os.environ.copy())
        if p.returncode == 3:
            return (cell, "INFRA_ABORT", p.stderr[-500:])
        if p.returncode != 0 and not os.path.exists(out):
            rec = {"arm": arm, "model": model, "task": task_id, "rep": rep,
                   "score": 0.0, "error": f"worker exit {p.returncode}",
                   "notes": [p.stderr[-400:]], "api_turns": None,
                   "total_tokens": None, "wall_s": round(time.time() - t0, 1),
                   "bridge_calls": None, "tool_calls_total": None,
                   "tool_counts": {}, "raw_xml_noise": False}
            with open(out, "w", encoding="utf-8") as f:
                json.dump(rec, f, indent=1)
            return (cell, "WORKER_ERR", p.stderr[-300:])
        return (cell, "OK",
                p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "")
    except subprocess.TimeoutExpired:
        rec = {"arm": arm, "model": model, "task": task_id, "rep": rep,
               "score": 0.0, "error": "wall timeout",
               "notes": ["hard wall timeout"], "api_turns": None,
               "total_tokens": None, "wall_s": round(time.time() - t0, 1),
               "bridge_calls": None, "tool_calls_total": None,
               "tool_counts": {}, "raw_xml_noise": False}
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1)
        return (cell, "TIMEOUT", "")


def record_checkpoint(result, store: CheckpointStore, lock: threading.Lock):
    """按运行结果落 checkpoint（lock 串行化进程内并发写，线程安全）。

    - OK → ok；
    - WORKER_ERR / TIMEOUT（已写 errored 记录 json）→ 墓碑（resume 默认
      跳过，--retry-tombstones 重跑；对齐 K6 语义，替代旧"删错重跑"）；
    - INFRA_ABORT（无记录）→ 不落任何记录：下次 resume 重跑（对齐旧
      "无文件重跑"行为，避免把没跑过的 cell 藏进墓碑）。
    """
    cell, status, info = result
    _arm, _task_id, _rep, _out, key = cell
    with lock:
        if status == "OK":
            store.append({"key": key, "status": "ok"})
        elif status == "WORKER_ERR":
            store.tombstone(key, info or "WORKER_ERR")
        elif status == "TIMEOUT":
            store.tombstone(key, "wall timeout")
        # INFRA_ABORT：故意不落记录


def run_battery(cells, store: CheckpointStore, lock: threading.Lock,
                model, py, parallel=4):
    """ThreadPoolExecutor 并行跑 cell，完成即 checkpoint（record_checkpoint）。

    并发纪律（K7-c）：store 写只在持有 lock 时发生（进程内线程安全）。
    单写者约束（K6 P2②）：跨进程不保证——不得有两个编排进程同时写同一
    results 目录的 checkpoint。
    """
    done = 0
    infra_aborts = 0
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {ex.submit(run_cell, c, model, py): c for c in cells}
        for fut in as_completed(futs):
            result = fut.result()
            record_checkpoint(result, store, lock)
            cell, status, info = result
            done += 1
            print(f"[{done}/{len(cells)}] {cell[0]}/{cell[1]}/rep{cell[2]}: "
                  f"{status} {info}", flush=True)
            if status == "INFRA_ABORT":
                infra_aborts += 1
                if infra_aborts >= 3:
                    print("FATAL: 3 infra aborts — stopping battery",
                          flush=True)
                    sys.exit(3)
    print("BATTERY COMPLETE", flush=True)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    model = args[0]
    reps = int(args[1])
    taskmod = _load_taskmod()
    task_ids = [t["id"] for t in taskmod.TASKS]
    arms = ["base", "pr"]
    parallel = 4
    retry_tombstones = False
    for a in args[2:]:
        if a.startswith("--tasks="):
            task_ids = a.split("=", 1)[1].split(",")
        elif a.startswith("--arms="):
            arms = a.split("=", 1)[1].split(",")
        elif a.startswith("--parallel="):
            parallel = int(a.split("=", 1)[1])
        elif a == "--retry-tombstones":
            retry_tombstones = True

    short = model.split("/")[-1]
    results = os.path.join(
        os.environ.get("ABDEFER_RESULTS",
                       os.path.join(HARNESS, "results")), short)
    os.makedirs(results, exist_ok=True)
    py = os.environ.get("ABDEFER_PYTHON", sys.executable)

    store = CheckpointStore(results)
    lock = threading.Lock()
    if not store.exists():
        bootstrap_from_results(results, store, model, taskmod.TASKS_BY_ID)
    cells = plan_cells(store, results, model, task_ids, arms, reps,
                       retry_tombstones=retry_tombstones)
    print(f"model={model} cells to run: {len(cells)} (parallel={parallel})",
          flush=True)
    run_battery(cells, store, lock, model, py, parallel=parallel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
