"""Run the read-tool eval through the REAL Hermes AIAgent.

For each task: fresh temp HERMES_HOME, fresh fixture workspace, real
AIAgent with the file+terminal+search toolsets, real provider API. Collects
accuracy plus efficiency metrics (API turns, tool calls, read_file calls,
prompt/completion tokens, wall time).

Usage:
  python3 evals/readtool/runner.py --model anthropic/claude-opus-4.8 \\
      --provider nous --reps 3 --label baseline
  python3 evals/readtool/runner.py --model qwen/qwen3.8-max \\
      --provider openrouter --reps 3 --label baseline --tasks fifo_hang

Results land in evals/readtool/results/<label>/<model-slug>/rep<N>.json.
Compare two labels with report.py.

Resume (K7-b, docs/Agent/13 §13; helper=../_resume.py): each cell
(task_id × rep) is appended line-by-line to rep<N>.jsonl (write+flush+fsync)
and tracked in <model-slug>/.checkpoint.jsonl with a content-addressed key
(content_key(task, label, model, prompt) + "::rep<N>") — an interrupted rep
resumes by running only its remaining cells. When every cell of a rep is
done, the legacy aggregate rep<N>.json is (re)exported from the JSONL rows
merged keep-first over any pre-K7 records, so report.py and other consumers
of rep*.json keep working unchanged. Historic rep<N>.json dirs without a
checkpoint bootstrap it: good/attempted records → done, errored records →
tombstone (skip on resume; --retry-tombstones re-runs them).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent.parent
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(EVAL_DIR.parent))  # 共享 helper：services/evals/_resume.py

from _resume import CheckpointStore, append_line, content_key  # noqa: E402

SYSTEM_SUFFIX = (
    "You are working inside the project directory {ws}. All paths in the "
    "task are relative to it. Work autonomously; do not ask questions. "
    "When done, state your final answer plainly."
)


def cell_key(task_id, label, model, prompt, rep):
    """内容寻址 cell key：content_key(task, label(=arm), model, prompt) + rep 后缀。

    与 session_search_schema/runner.cell_key 同构（docs/Agent/13 §11/§13）：
    rep 是显式重复维度，不进哈希、以固定后缀拼接；prompt 一变 key 即变。
    """
    return f"{content_key(task_id, label, model, prompt)}::rep{rep}"


def _count_metrics(messages: list) -> dict:
    api_turns = 0
    tool_calls = 0
    read_calls = 0
    read_errors = 0
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            api_turns += 1
            for tc in m.get("tool_calls") or []:
                tool_calls += 1
                fn = (tc.get("function") or {}).get("name", "")
                if fn == "read_file":
                    read_calls += 1
        elif role == "tool":
            content = m.get("content") or ""
            if isinstance(content, list):
                content = " ".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                )
            if '"error"' in content or "File not found" in content:
                read_errors += 1
    return {
        "api_turns": api_turns,
        "tool_calls": tool_calls,
        "read_file_calls": read_calls,
        "tool_error_results": read_errors,
    }


def run_task(task, model: str, provider: str, timeout_mult: float,
             toolsets: list[str]) -> dict:
    # 延迟导入（K7-b）：模块导入面不触碰本目录平铺名的 tasks/fixtures——
    # 其他 eval 模块（session_search_schema）在同名下有不同内容，同进程
    # 共存（pytest 收集）时避免 sys.modules 串包。
    from fixtures import build_workspace  # noqa: PLC0415

    ws = Path(tempfile.mkdtemp(prefix=f"readtool-{task.task_id}-"))
    hermes_home = Path(tempfile.mkdtemp(prefix="readtool-home-")) / ".hermes"
    hermes_home.mkdir(parents=True)
    build_workspace(ws)

    old_env = dict(os.environ)
    os.environ["HERMES_HOME"] = str(hermes_home)
    os.environ["TERMINAL_CWD"] = str(ws)
    # Keep only the API key the run needs; hide the rest so provider
    # auto-detection can't wander (mirrors run_tests.sh hermeticity).
    for var in list(os.environ):
        if var.endswith("_API_KEY") and var != "OPENROUTER_API_KEY":
            os.environ.pop(var)
    result: dict = {"task_id": task.task_id, "capability": task.capability}
    t0 = time.monotonic()
    try:
        # Import inside the env so profile-aware paths bind to the temp home.
        from run_agent import AIAgent  # noqa: PLC0415

        agent = AIAgent(
            model=model,
            provider=provider,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            enabled_toolsets=toolsets,
            max_iterations=40,
        )
        convo = agent.run_conversation(
            SYSTEM_SUFFIX.format(ws=ws) + "\n\nTask: " + task.prompt,
        )
        final = convo.get("final_response") or ""
        messages = convo.get("messages") or []
        result.update(_count_metrics(messages))
        result.update(
            {
                "final_response": final,
                "score": task.grade(final),
                "prompt_tokens": getattr(agent, "session_prompt_tokens", 0),
                "completion_tokens": getattr(agent, "session_completion_tokens", 0),
                "total_tokens": getattr(agent, "session_total_tokens", 0),
                "wall_s": round(time.monotonic() - t0, 1),
                "error": None,
            }
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        if "No LLM provider configured" in str(exc) or "authentication" in str(exc).lower():
            # Harness misconfiguration, not a model result. Abort the whole
            # run rather than writing poisoned zero-score records.
            raise SystemExit(f"ABORT (harness config error, not a result): {msg}")
        result.update(
            {
                "final_response": "",
                "score": 0.0,
                "wall_s": round(time.monotonic() - t0, 1),
                "error": msg,
            }
        )
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(hermes_home.parent, ignore_errors=True)
    return result


def _bootstrap_done_from_legacy(out_dir: Path, store: CheckpointStore,
                                label: str, model: str,
                                tasks_by_id: dict) -> None:
    """升级引导：模型目录尚无 checkpoint 时，从历史 rep<N>.json 回填。

    与 session_search_schema/runner._bootstrap_done_from_output 同构（K6）：
    good/attempted 记录（error 为空或 score>0，沿用旧编排的保留口径）→
    物化 done；errored 记录 → 墓碑（resume 默认跳过，--retry-tombstones
    重跑）；task 已不在电池（无 prompt 可算 key）或整文件损坏的行跳过
    ——对应 cell 会重跑一次，append-only 输出可接受。
    """
    for json_path in sorted(out_dir.glob("rep*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue  # 损坏聚合文件：该 rep 的 cell 全部重跑
        rep = data.get("rep")
        if rep is None:
            continue
        for rec in data.get("records") or []:
            task_id = rec.get("task_id")
            task = tasks_by_id.get(task_id)
            if task is None:
                continue
            key = cell_key(task_id, label, model, task.prompt, rep)
            if rec.get("error"):
                store.tombstone(key, str(rec["error"]))
            else:
                store.append({"key": key, "status": "ok",
                              "src": "bootstrap"})


def _load_rep_records(out_dir: Path, rep: int) -> list[dict]:
    """聚合读取一个 rep 的记录：rep<N>.jsonl 增量行优先，旧 rep<N>.json 兜底。

    task_id keep-first 去重——JSONL 行在前（重跑/续跑的新记录覆盖旧 json
    里的同 cell 记录），旧 json 记录在后（仅补 JSONL 没有的历史 cell）；
    JSONL 内部崩溃窗口重复行同样 keep-first（保留首行=首次结果）。
    """
    records: list[dict] = []
    seen: set = set()
    jsonl_path = out_dir / f"rep{rep}.jsonl"
    if jsonl_path.exists():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue  # 尾行截断等损坏行：跳过
            tid = rec.get("task_id")
            if tid is None or tid in seen:
                continue
            seen.add(tid)
            records.append(rec)
    json_path = out_dir / f"rep{rep}.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            data = None
        for rec in (data or {}).get("records") or []:
            tid = rec.get("task_id")
            if tid is None or tid in seen:
                continue
            seen.add(tid)
            records.append(rec)
    return records


def _export_rep_json(out_dir: Path, rep: int, model: str, provider: str,
                     label: str) -> Path:
    """把一个 rep 的合并记录导出为旧格式聚合 rep<N>.json（report.py 兼容面）。

    聚合 json 是派生数据（真值在 rep<N>.jsonl + checkpoint），不 fsync；
    中途写坏可由下次续跑按"json 缺失→重导出"自愈。
    """
    json_path = out_dir / f"rep{rep}.json"
    json_path.write_text(
        json.dumps({"model": model, "provider": provider, "label": label,
                    "rep": rep, "records": _load_rep_records(out_dir, rep)},
                   indent=2),
        encoding="utf-8")
    return json_path


def run_battery(run_task_fn, model: str, provider: str, label: str,
                timeout_mult: float, toolsets: list[str], slate: list,
                out_dir: Path, store: CheckpointStore, reps: int = 1,
                retry_tombstones: bool = False) -> None:
    """逐 cell (task × rep) 断点续跑（K7-b；蓝本=K6 session_search run_battery）。

    - 跳过判定只读 checkpoint（输出与 checkpoint 解耦）；每 cell 完成即
      append_line 到 rep<N>.jsonl（write+flush+fsync）并 checkpoint ok——
      rep 中途断，重跑只补剩余 cell，已确认行不重跑不重写；
    - rep 的 cell 全部完成后聚合导出 rep<N>.json（兼容面）；崩溃窗口
      （末 cell 已 append、聚合未写）在续跑时补写；
    - run_task_fn 抛 SystemExit（harness 配置错误）原样上抛中止整跑，
      该 cell 不落任何记录（沿用旧语义，不墓碑）。
    """
    store.load()
    out_dir.mkdir(parents=True, exist_ok=True)
    for rep in range(1, reps + 1):
        jsonl_path = out_dir / f"rep{rep}.jsonl"
        json_path = out_dir / f"rep{rep}.json"
        keys = {t.task_id: cell_key(t.task_id, label, model, t.prompt, rep)
                for t in slate}
        pending = [t for t in slate
                   if not store.is_blocked(keys[t.task_id],
                                           retry_tombstones=retry_tombstones)]
        if not pending and json_path.exists():
            print(f"rep{rep} complete, skipping")
            continue
        with open(jsonl_path, "a", encoding="utf-8") as f:
            for task in pending:
                print(f"[rep{rep}] {task.task_id} ...", flush=True)
                rec = run_task_fn(task, model, provider, timeout_mult,
                                  toolsets)
                rec["rep"] = rep
                append_line(f, rec)
                store.append({"key": keys[task.task_id], "status": "ok"})
                print(
                    f"[rep{rep}] {task.task_id}: score={rec['score']:.2f} "
                    f"turns={rec.get('api_turns', '?')} "
                    f"tok={rec.get('total_tokens', '?')} "
                    f"wall={rec['wall_s']}s err={rec.get('error')}",
                    flush=True,
                )
        if (all(store.is_blocked(keys[t.task_id]) for t in slate)
                and (pending or not json_path.exists())):
            # 全部 cell 完成才导出；本 rep 有新行，或 json 缺失（"末 cell 已
            # append、聚合未写"的崩溃窗口续跑）时（重）导出。
            _export_rep_json(out_dir, rep, model, provider, label)
            print(f"wrote {json_path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--label", required=True, help="e.g. baseline, feat-fifo-guard")
    ap.add_argument("--tasks", default="", help="comma-separated task ids (default all)")
    ap.add_argument("--timeout-mult", type=float, default=1.0)
    ap.add_argument(
        "--toolsets",
        default="file,terminal,search",
        help=(
            "Comma-separated toolsets. Use 'file' alone for the "
            "discriminative arm (no terminal escape hatch — the read tool "
            "must handle the hostile file itself)."
        ),
    )
    ap.add_argument(
        "--retry-tombstones", action="store_true", default=False,
        help="re-run cells whose bootstrap legacy record errored into a "
             "tombstone (default: skip them on resume)")
    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit(
            "OPENROUTER_API_KEY not in environment. Run: set -a; "
            "source ~/.hermes/.env; set +a  — then relaunch."
        )

    # 延迟导入（K7-b，理由见 run_task 注释）：平铺名 tasks 仅在 CLI 真跑时
    # 才加载（脚本方式运行时 sys.path[0]=本目录，拿到的是本目录 tasks）。
    from tasks import TASKS, TASKS_BY_ID  # noqa: PLC0415

    slate = (
        [TASKS_BY_ID[t] for t in args.tasks.split(",") if t]
        if args.tasks
        else TASKS
    )
    slug = args.model.replace("/", "_")
    out_dir = EVAL_DIR / "results" / args.label / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    store = CheckpointStore(out_dir)
    if not store.exists():
        _bootstrap_done_from_legacy(out_dir, store, args.label, args.model,
                                    TASKS_BY_ID)

    run_battery(run_task, args.model, args.provider, args.label,
                args.timeout_mult, [t for t in args.toolsets.split(",") if t],
                slate, out_dir, store, reps=args.reps,
                retry_tombstones=args.retry_tombstones)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
