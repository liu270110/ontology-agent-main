"""Live A/B runner: session_search schema variants, extracted from git refs.

For each arm, ``tools/session_search_tool.py`` is extracted from a git ref
(``git show <ref>:tools/session_search_tool.py``) and imported as its own
module. A minimal agent loop (OpenRouter, tools API) then runs the shared
task battery against a freshly seeded temp session DB. The ONLY variable
between arms is that module — schema text, response hints, tool behavior.

Usage:
  python3 evals/session_search_schema/runner.py \
      --base origin/main --cand HEAD \
      --model qwen/qwen3-coder-30b-a3b-instruct --reps 3

  # limit to one task
  ... --tasks t2_scroll

Results append to results/<label>/<model-slug>.jsonl. Resume-safe via
checkpoint (K6-b, helper=_resume.py): completed cells are tracked in
results/<label>/.checkpoint.jsonl (content-addressed key: task/arm/model/prompt
+ rep) and skipped on re-run — output rows and checkpoint are decoupled. Cells
that exhaust retries with an exception are tombstoned and skipped on resume;
pass --retry-tombstones to re-run them. Summarize with report.py.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent.parent
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(EVAL_DIR.parent))  # 共享 helper：services/evals/_resume.py

from tasks import SYSTEM, TASKS  # noqa: E402
from _resume import CheckpointStore, append_line, content_key  # noqa: E402

ALLOWED_KEYS = {
    "query", "role_filter", "limit", "session_id", "around_message_id",
    "window", "sort", "profile", "detail",
}


def _load_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("OPENROUTER_API_KEY not found (env or ~/.hermes/.env)")


def extract_arm(ref: str, workdir: Path, name: str) -> Path:
    """Extract tools/session_search_tool.py from a git ref."""
    out = subprocess.run(
        ["git", "show", f"{ref}:tools/session_search_tool.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"git show {ref}: {out.stderr.strip()}")
    path = workdir / f"ss_arm_{name}.py"
    path.write_text(out.stdout)
    return path


def load_arm(path: Path, name: str, work_db_path: Path):
    """Import an arm module and make profile resolution hermetic."""
    from hermes_state import SessionDB

    spec = importlib.util.spec_from_file_location(f"ss_arm_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"ss_arm_{name}"] = mod
    spec.loader.exec_module(mod)

    def _fake_resolve_profile_db(profile):
        if profile is None or not str(profile).strip():
            return None
        if str(profile).strip().lower() == "work":
            return SessionDB(db_path=work_db_path, read_only=True)
        raise ValueError(f"profile '{profile}' does not exist")

    mod._resolve_profile_db = _fake_resolve_profile_db
    return mod


def build_tools(arm_mod):
    s = arm_mod.SESSION_SEARCH_SCHEMA
    return [{
        "type": "function",
        "function": {
            "name": s["name"],
            "description": s["description"],
            "parameters": s["parameters"],
        },
    }]


def exec_tool(arm_mod, args, main_db_path: Path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=main_db_path)
    try:
        kwargs, bad = {}, []
        for k, v in args.items():
            if k in ALLOWED_KEYS:
                kwargs[k] = v
            else:
                bad.append(k)
        if bad:
            return json.dumps({
                "success": False,
                "error": f"unexpected parameter(s): {', '.join(bad)}",
            }), True
        return arm_mod.session_search(db=db, **kwargs), False
    except Exception as e:  # noqa: BLE001 — tool errors go back to the model
        return json.dumps({
            "success": False, "error": f"{type(e).__name__}: {e}",
        }), True
    finally:
        try:
            db.close()
        except Exception:
            pass


def run_one(client, model, arm_name, arm_mod, task_id, prompt, oracle,
            main_db_path: Path, max_iters: int = 8):
    tools = build_tools(arm_mod)
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt}]
    calls, bad_calls = [], 0
    first_prompt_tokens, total_tokens = None, 0
    final = ""
    t0 = time.time()
    for _ in range(max_iters):
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=tools,
            temperature=0.2, max_tokens=2000,
        )
        u = getattr(resp, "usage", None)
        if u:
            if first_prompt_tokens is None:
                first_prompt_tokens = u.prompt_tokens
            total_tokens += (u.total_tokens or 0)
        msg = resp.choices[0].message
        tcs = msg.tool_calls or []
        if not tcs:
            final = msg.content or ""
            break
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name,
                              "arguments": tc.function.arguments}}
                for tc in tcs
            ],
        })
        for tc in tcs:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args, bad_calls = {}, bad_calls + 1
            calls.append(args)
            if tc.function.name != "session_search":
                out, was_err = json.dumps(
                    {"success": False, "error": "unknown tool"}), True
            else:
                out, was_err = exec_tool(arm_mod, args, main_db_path)
            if was_err:
                bad_calls += 1
            if len(out) > 30000:
                out = out[:30000] + "...[truncated]"
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": out})
    return {
        "task": task_id, "arm": arm_name, "model": model,
        "ok": bool(oracle(final)) if final else False,
        "n_tool_calls": len(calls), "bad_calls": bad_calls,
        "first_prompt_tokens": first_prompt_tokens,
        "total_tokens": total_tokens,
        "wall_s": round(time.time() - t0, 1),
        "calls": calls, "final": final[:2000],
    }


def cell_key(task_id, arm_name, model, prompt, rep):
    """内容寻址 cell key：content_key(task, arm, model, prompt) + rep 维度。

    rep 是实验设计矩阵的显式重复维度而非顺序索引，不进哈希、以固定后缀拼接
    （同 (task, arm, model, prompt) 的各 rep 互不遮挡）；prompt 一变
    content_key 即变，历史 checkpoint 不遮挡新输入。
    """
    return f"{content_key(task_id, arm_name, model, prompt)}::rep{rep}"


def _bootstrap_done_from_output(outpath: Path, store: CheckpointStore,
                                model: str, tasks: dict) -> None:
    """升级引导：label 目录尚无 checkpoint 时，从历史输出 JSONL 回填 done。

    历史行含 task/arm/rep（model 即本次 --model），逐行重算 cell key 并物化
    进 checkpoint（status=ok, src=bootstrap）；损坏行、缺 rep 或 task 已从
    TASKS 移除的行跳过（对应 cell 会重跑一次，append-only 输出可接受）。
    """
    if not outpath.exists():
        return
    for line in outpath.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        task_id = r.get("task")
        rep = r.get("rep")
        if task_id not in tasks or rep is None:
            continue
        key = cell_key(task_id, r.get("arm"), model, tasks[task_id][0], rep)
        store.append({"key": key, "status": "ok", "src": "bootstrap"})


def run_battery(client, model, arms, tasks, outpath: Path,
                store: CheckpointStore, main_db,
                tasks_filter=None, reps: int = 3,
                retry_tombstones: bool = False,
                base_ref=None, cand_ref=None, max_attempts: int = 3) -> None:
    """批跑 (task, rep, arm) 电池：checkpoint 驱动断点续跑。

    蓝本 hermes batch_runner.py:542-572（增量 checkpoint + 内容寻址 resume）、
    :339-347（墓碑）。跳过判定读 checkpoint（输出 JSONL 与 checkpoint 解耦，
    输出行照旧追加）；每条结果写入后 checkpoint.append({key, status:"ok"})；
    重试耗尽仍异常的 cell 记墓碑（resume 默认跳过，retry_tombstones 重跑）。
    中断（如 KeyboardInterrupt）不落任何记录，cell 留待下次续跑。
    """
    store.load()
    outpath.parent.mkdir(parents=True, exist_ok=True)
    with open(outpath, "a", encoding="utf-8") as f:
        for task_id, (prompt, oracle, _note) in tasks.items():
            if tasks_filter and task_id not in tasks_filter:
                continue
            for rep in range(reps):
                for arm_name, arm_mod in arms.items():
                    key = cell_key(task_id, arm_name, model, prompt, rep)
                    if store.is_blocked(key,
                                        retry_tombstones=retry_tombstones):
                        continue
                    last_err = None
                    for attempt in range(max_attempts):
                        try:
                            r = run_one(client, model, arm_name, arm_mod,
                                        task_id, prompt, oracle, main_db)
                            # Provider noise: zero tool calls AND empty
                            # final → one retry, identical on both arms.
                            if (not r["final"].strip()
                                    and r["n_tool_calls"] == 0
                                    and attempt < max_attempts - 1):
                                print(f"NOISE-RETRY {task_id} {arm_name} "
                                      f"rep{rep}")
                                continue
                            r["rep"] = rep
                            r["base_ref"] = base_ref
                            r["cand_ref"] = cand_ref
                            append_line(f, r)
                            store.append({"key": key, "status": "ok"})
                            print(f"{task_id} {arm_name} rep{rep}: "
                                  f"ok={r['ok']} calls={r['n_tool_calls']} "
                                  f"bad={r['bad_calls']} "
                                  f"ptok={r['first_prompt_tokens']}")
                            break
                        except Exception as e:  # noqa: BLE001
                            last_err = e
                            print(f"RETRY {task_id} {arm_name} rep{rep}: {e}")
                            traceback.print_exc()
                            time.sleep(5 * (attempt + 1))
                    else:
                        reason = (f"{type(last_err).__name__}: {last_err}"
                                  if last_err else "retries exhausted")
                        store.tombstone(key, reason)
                        print(f"TOMBSTONE {task_id} {arm_name} rep{rep}: "
                              f"{reason}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="git ref for the baseline arm")
    ap.add_argument("--cand", required=True, help="git ref for the candidate arm")
    ap.add_argument("--model", required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--tasks", nargs="*", default=None)
    ap.add_argument("--label", default="ab")
    ap.add_argument("--retry-tombstones", action="store_true", default=False,
                    help="re-run cells that errored into tombstones "
                         "(default: skip them on resume)")
    args = ap.parse_args()

    from openai import OpenAI
    client = OpenAI(base_url="https://openrouter.ai/api/v1",
                    api_key=_load_api_key())

    with tempfile.TemporaryDirectory(prefix="ss_abeval_") as td:
        tdir = Path(td)
        from fixtures import seed
        dbdir = tdir / "dbs"
        seed(dbdir)
        main_db = dbdir / "state.db"
        work_db = dbdir / "state_work.db"

        arms = {
            "base": load_arm(extract_arm(args.base, tdir, "base"), "base", work_db),
            "cand": load_arm(extract_arm(args.cand, tdir, "cand"), "cand", work_db),
        }

        outdir = EVAL_DIR / "results" / args.label
        outdir.mkdir(parents=True, exist_ok=True)
        outpath = outdir / (re.sub(r"[^\w.-]", "_", args.model) + ".jsonl")
        store = CheckpointStore(outdir)
        if not store.exists():
            _bootstrap_done_from_output(outpath, store, args.model, TASKS)
        run_battery(client, args.model, arms, TASKS, outpath, store, main_db,
                    tasks_filter=args.tasks, reps=args.reps,
                    retry_tombstones=args.retry_tombstones,
                    base_ref=args.base, cand_ref=args.cand)
        print("done ->", outpath)


if __name__ == "__main__":
    main()
