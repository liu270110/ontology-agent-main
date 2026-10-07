"""ontology-scale suite 执行器（docs/Agent/16 §1 G 域；问题源=红队审查 G1/F3）。

形态（16 篇 §1）：generator.py 合成本体梯度（确定性种子）→ runner 跑三档场景收三指标 →
metrics.py 纯函数口径出数。三场景 = 规模三档 scale_1e2/1e3/1e4（类数 10²/10³/10⁴，
属性形状 2N，实例 10N），每档三指标：

    ① validate_latency        G1：pySHACL 校验时延（平台 services/ontology/core/shacl.validate
                              同参：advanced=True/inference=none；干净/带违例两形态）
    ② assemble_token_cost     G1：TBox 注入上下文成本（grounding 组装口径：类名+属性清单摘要
                              vs 全量 schema 两模式，token 计数后端可配）
    ③ reindex_consistency     F3：改 TBox 属性定义→旧实例校验失败检出率（三契约型变异 +
                              TBox-only 对照——对照型在 inference=none 下属已知盲区，单列不计分母）

超时纪律（10⁴ 档可能慢）：每档墙钟预算 tier_timeout_s（Settings/CLI 可调），预算尽即停，
已完成指标如实落盘、缺的记 partial——不伪造完成、不静默截断。
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

if sys.platform == "win32":  # 平台一致性（本套件无 psycopg 依赖，随 run.py 惯例固定）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # 仓库根（services.* 平台校验路径导入面）

from services.ontology.core.shacl import validate as platform_validate  # noqa: E402

_SIBLING = Path(__file__).resolve().parent


def _import_sibling(module_name: str, file_name: str) -> Any:
    """同目录模块加载（目录名含连字符不可作包名，importlib 文件位加载；agent-core runner 同款）。"""
    spec = importlib.util.spec_from_file_location(module_name, str(_SIBLING / file_name))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


generator = _import_sibling("bench_onto_scale_generator", "generator.py")
metrics = _import_sibling("bench_onto_scale_metrics", "metrics.py")
config_mod = _import_sibling("bench_onto_scale_config", "config.py")

BENCHMARK_REF = "docs/Agent/16-基准对比体系设计 §2 + docs/评审/红队攻击性审查-2026-10-06 G1/F3"

# 场景 = 规模档（场景名 → 类数）
SCENARIOS: dict[str, int] = {"scale_1e2": 100, "scale_1e3": 1000, "scale_1e4": 10000}

# 摘要关键指标（SUMMARY.md 单列展示：干净 ABox 校验 p95——门禁高频路径的成本面）
KEY_METRIC: dict[str, str] = {name: "validate_clean_p95_ms" for name in SCENARIOS}

ORSI_FACE_SUGGESTION: dict[str, str] = {name: "O2" for name in SCENARIOS}  # O2 本体面（orsi_link 同源）

# F3 断言白名单（防作弊断言纪律：只允许对 metrics 产出键下断言）
_ALLOWED_METRIC_KEYS = frozenset(
    {
        "violation_hit_exact",
        "detection_rate",
        "compression_ratio",
        "clean_conforms",
        "violations_nonconforms",
    }
)


# ---------------------------------------------------------------------------
# token 计数后端（IO 面；metrics.py 保持纯函数）
# ---------------------------------------------------------------------------


async def count_units(
    units: Sequence[str],
    *,
    backend: str,
    vllm_base_url: str = "",
    vllm_model: str = "",
    batch_size: int = 50,
    prefix: str = "",
) -> int:
    """组装单元序列 → 总 token 数（注入文本=prefix+"\n".join(units) 的真实分词口径）。

    vllm 档按批合并计数（10⁴ 单元单条逐数会打 1 万次 HTTP；批内合并文本即真实注入形态，
    批间边界与批内同为 "\n" 连接）。服务不可达/非 200 → RuntimeError 上抛不静默降级
    （计数口径突变属指标口径变化，rag suite 同纪律）。
    """
    if backend == "heuristic":
        import re

        cjk = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
        ascii_word = re.compile(r"[A-Za-z0-9_]+")

        def _estimate(text: str) -> int:
            return len(cjk.findall(text)) + len(ascii_word.findall(text))

        return _estimate(prefix + "\n".join(units))
    if backend != "vllm":
        raise ValueError(f"未知 token_counter: {backend!r}（可选 heuristic|vllm）")
    total = 0
    async with httpx.AsyncClient(timeout=60.0) as client:
        for start in range(0, len(units), batch_size):
            batch = units[start : start + batch_size]
            # 批间边界同样以 "\n" 连接（批首补分隔符）——否则批间换行不入任何请求，总数少计
            text = (prefix if start == 0 else "\n") + "\n".join(batch)
            if not text:
                continue
            resp = await client.post(
                f"{vllm_base_url.rstrip('/')}/tokenize", json={"model": vllm_model, "prompt": text}
            )
            if resp.status_code != 200:
                raise RuntimeError(f"vLLM /tokenize 失败：HTTP {resp.status_code} {resp.text[:120]}")
            total += int(resp.json().get("count", 0))
    return total


# ---------------------------------------------------------------------------
# 单档执行（生成→①校验时延→②token 成本→③F3 检出；deadline=档墙钟预算锚）
# ---------------------------------------------------------------------------


async def _run_tier(
    scale: int,
    settings: Any,
    deadline: float | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    """单档执行核心；返回 (metrics, samples, partial_note)。partial_note=None=全指标完成。"""
    notes: list[str] = []
    samples: list[dict[str, Any]] = []

    def _expired() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def _remain_s() -> float | None:
        return None if deadline is None else round(deadline - time.monotonic(), 1)

    # 生成（确定性；10⁴ 档实测 ~10s 量级）
    onto = generator.generate(
        scale,
        seed=settings.seed,
        instance_multiplier=settings.instance_multiplier,
        violation_ratio=settings.violation_ratio,
    )
    metrics_out: dict[str, Any] = {**onto.counts, "scale": scale}

    # ① validate_latency：干净/带违例两形态 × repeats（平台校验路径原样直喂）
    if _expired():
        notes.append(f"生成后预算尽（剩 {_remain_s()}s）：validate_latency/token/reindex 全缺")
        return metrics_out, samples, "; ".join(notes)
    clean_ms: list[float] = []
    violations_ms: list[float] = []
    clean_conforms: bool | None = None
    violations_nonconforms: bool | None = None
    violations_found = -1
    for repeat in range(settings.latency_repeats):
        if _expired() and (clean_ms or violations_ms):
            notes.append(f"validate_latency 于 repeat={repeat} 触顶（剩 {_remain_s()}s），部分采样入数")
            break
        report = platform_validate(onto.abox_clean, onto.shapes, tbox_graph=onto.tbox)
        clean_ms.append(float(report.elapsed_ms))
        clean_conforms = report.conforms
        report_v = platform_validate(onto.abox_violations, onto.shapes, tbox_graph=onto.tbox)
        violations_ms.append(float(report_v.elapsed_ms))
        violations_nonconforms = not report_v.conforms
        violations_found = len(report_v.results)
        samples.append(
            {
                "phase": "validate_latency",
                "repeat": repeat,
                "clean_ms": report.elapsed_ms,
                "violations_ms": report_v.elapsed_ms,
                "clean_conforms": report.conforms,
                "violations_found": len(report_v.results),
            }
        )
    if clean_ms:
        latency = metrics.validate_latency(
            clean_ms,
            violations_ms,
            violations_found=violations_found,
            violations_expected=onto.violations_expected,
        )
        metrics_out["validate_latency"] = latency
        metrics_out.update(
            {
                "validate_clean_p50_ms": latency["clean"]["p50_ms"],
                "validate_clean_p95_ms": latency["clean"]["p95_ms"],
                "validate_violations_p50_ms": latency["violations"]["p50_ms"],
                "validate_violations_p95_ms": latency["violations"]["p95_ms"],
                "validate_violation_overhead_ratio": latency["violation_overhead_ratio"],
                "violations_found": violations_found,
                "violations_expected": onto.violations_expected,
                "violation_hit_exact": latency["violation_hit_exact"],
                "clean_conforms": clean_conforms,
                "violations_nonconforms": violations_nonconforms,
            }
        )

    # ② assemble_token_cost：TBox 摘要 vs 全量 schema 两模式（grounding 组装口径）
    if _expired():
        notes.append(f"token 成本阶段预算尽（剩 {_remain_s()}s）跳过")
    else:
        summary_tokens = await count_units(
            onto.summary_units,
            backend=settings.token_counter,
            vllm_base_url=settings.vllm_base_url,
            vllm_model=settings.vllm_model,
            prefix=onto.prolog,
        )
        full_tokens = await count_units(
            onto.full_units,
            backend=settings.token_counter,
            vllm_base_url=settings.vllm_base_url,
            vllm_model=settings.vllm_model,
            prefix=onto.prolog,
        )
        cost = metrics.assemble_token_cost(
            summary_tokens,
            full_tokens,
            summary_chars=len(onto.summary_text()),
            full_chars=len(onto.full_schema_text()),
            class_count=onto.counts["classes"],
            counter_backend=settings.token_counter,
        )
        metrics_out["assemble_token_cost"] = cost
        metrics_out.update(
            {
                "summary_tokens": cost["summary_tokens"],
                "full_tokens": cost["full_tokens"],
                "compression_ratio": cost["compression_ratio"],
            }
        )
        samples.append(
            {
                "phase": "assemble_token_cost",
                "summary_tokens": summary_tokens,
                "full_tokens": full_tokens,
                "backend": settings.token_counter,
            }
        )

    # ③ reindex_consistency：F3 变异探针（三契约型 + TBox-only 对照）
    if _expired():
        notes.append(f"F3 变异阶段预算尽（剩 {_remain_s()}s）跳过")
    else:
        records: list[dict[str, Any]] = []
        for target in generator.mutation_targets(onto)[: settings.reindex_mutations]:
            mutated_shapes, mutated_tbox, desc = generator.apply_mutation(onto, target)
            report = platform_validate(onto.abox_clean, mutated_shapes, tbox_graph=mutated_tbox)
            records.append(
                {
                    "kind": target["kind"],
                    "class_local": target["class_local"],
                    "detected": not report.conforms,
                    "violations_after": len(report.results),
                    "description": desc,
                }
            )
            samples.append({"phase": "reindex", **records[-1]})
        f3 = metrics.reindex_consistency(records, contract_kinds=generator.MUTATION_KINDS)
        metrics_out["reindex_consistency"] = f3
        metrics_out.update(
            {
                "detection_rate": f3["detection_rate"],
                "contract_mutations": f3["contract_mutations"],
                "tbox_only_visible": f3["tbox_only_visible"],
            }
        )

    return metrics_out, samples, ("; ".join(notes) if notes else None)


# ---------------------------------------------------------------------------
# 基准断言（防作弊断言纪律：白名单键 + 受控操作符）
# ---------------------------------------------------------------------------


def eval_asserts(asserts: list[dict[str, Any]], metrics_out: dict[str, Any]) -> list[dict[str, Any]]:
    """断言求值（metrics 键缺失=断言失败并注明，不静默；agent-core 同款语义）。"""
    results: list[dict[str, Any]] = []
    for item in asserts:
        actual = metrics_out.get(item["metric"])
        if actual is None:
            passed = False
        else:
            passed = {
                ">=": actual >= item["value"],
                "<=": actual <= item["value"],
                "==": actual == item["value"],
                ">": actual > item["value"],
                "<": actual < item["value"],
            }[item["op"]]
        results.append({**item, "actual": actual, "passed": passed})
    return results


def benchmark_asserts() -> list[dict[str, Any]]:
    """套件级基准断言（G1/F3 的验收口径；失败=assert_failed 如实落盘不粉饰）。

    - violation_hit_exact：违例命中数=解析期望（shapes 有效性=门禁真能命中靶）；
    - clean_conforms / violations_nonconforms：门禁双形态语义成立；
    - detection_rate == 1.0：F3 契约型漂移全检出（漏检即平台门禁真实缺口，落 assert_failed）；
    - compression_ratio > 1：摘要面对全量 schema 有净 token 收益（G1 组装成本曲线的存在前提）。
    """
    return [
        {"metric": "violation_hit_exact", "op": "==", "value": True},
        {"metric": "clean_conforms", "op": "==", "value": True},
        {"metric": "violations_nonconforms", "op": "==", "value": True},
        {"metric": "detection_rate", "op": "==", "value": 1.0},
        {"metric": "compression_ratio", "op": ">", "value": 1.0},
    ]


# ---------------------------------------------------------------------------
# suite 运行入口（run.py 调用；契约同 agent-core：async run_suite → (results, manifest)）
# ---------------------------------------------------------------------------


async def run_suite(
    *, smoke: bool = False, only: str | None = None, overrides: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """执行 suite 全档：逐档生成+三指标（档间独立、单档失败不中止整体）→（结果列表, 运行清单）。

    smoke=True 跑全三档（16 篇 §2 曲线需要三档齐；档内参数不减配——超时预算兜底）。
    overrides=CLI/env 之上的 Settings 覆盖（run.py 传入；缺省读 BENCH_ONTO_SCALE_*）。
    """
    settings = config_mod.OntoScaleBenchSettings(**(overrides or {}))
    for tier in settings.tiers:
        if tier not in SCENARIOS.values():
            raise KeyError(f"未知规模档: {tier}（可选: {', '.join(map(str, SCENARIOS.values()))}）")
    results: list[dict[str, Any]] = []
    started_all = time.monotonic()
    for name, scale in SCENARIOS.items():
        if scale not in settings.tiers:
            continue
        if only is not None and name != only:
            continue
        deadline = time.monotonic() + settings.tier_timeout_s
        entry: dict[str, Any] = {
            "suite": "ontology-scale",
            "scenario": name,
            "benchmark_ref": BENCHMARK_REF,
            "orsi_face_suggestion": ORSI_FACE_SUGGESTION.get(name),
            "smoke": smoke,
            "started_at": _utcnow_iso(),
            "params": {
                "scale": scale,
                "seed": settings.seed,
                "instance_multiplier": settings.instance_multiplier,
                "violation_ratio": settings.violation_ratio,
                "latency_repeats": settings.latency_repeats,
                "reindex_mutations": settings.reindex_mutations,
                "tier_timeout_s": settings.tier_timeout_s,
                "token_counter": settings.token_counter,
            },
            "metrics": {},
            "samples": [],
            "asserts": [],
            "status": "ok",
            "error": None,
        }
        tier_started = time.monotonic()
        try:
            metrics_out, samples, partial_note = await _run_tier(scale, settings, deadline)
            entry["metrics"] = metrics_out
            entry["samples"] = samples
            if partial_note:
                entry["status"] = "partial"
                entry["partial_note"] = partial_note
            entry["asserts"] = eval_asserts(benchmark_asserts(), metrics_out)
            if any(not item["passed"] for item in entry["asserts"]) and entry["status"] == "ok":
                entry["status"] = "assert_failed"
            elif any(not item["passed"] for item in entry["asserts"]):
                entry["partial_note"] = (partial_note or "") + "（asserts 亦存在失败）"
        except Exception as exc:  # noqa: BLE001 ——单档失败不中止基准（结果留痕可归因）
            entry["status"] = "error"
            entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["duration_s"] = round(time.monotonic() - tier_started, 3)
        results.append(entry)
    manifest = {
        "suite": "ontology-scale",
        "smoke": smoke,
        "env": env_fingerprint(settings),
        "total_duration_s": round(time.monotonic() - started_all, 3),
        "scenarios_run": [r["scenario"] for r in results],
        "status_counts": {
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "partial": sum(1 for r in results if r["status"] == "partial"),
            "assert_failed": sum(1 for r in results if r["status"] == "assert_failed"),
            "error": sum(1 for r in results if r["status"] == "error"),
        },
        "finished_at": _utcnow_iso(),
    }
    return results, manifest


def env_fingerprint(settings: Any) -> dict[str, Any]:
    """环境指纹（16 篇 §3：commit/env/口径进结果 JSON——对比曲线的对齐轴）。"""

    def _git(args: list[str]) -> str:
        try:
            return subprocess.run(
                ["git", *args], capture_output=True, text=True, encoding="utf-8", timeout=10, check=True
            ).stdout.strip()
        except Exception:  # noqa: BLE001 ——非 git 环境留空不阻塞
            return ""

    import platform as _platform

    import pyshacl
    import rdflib

    return {
        "commit": _git(["rev-parse", "HEAD"]),
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "dirty": bool(_git(["status", "--porcelain"])),
        "python": _platform.python_version(),
        "platform": _platform.platform(),
        "pyshacl": pyshacl.__version__,
        "rdflib": rdflib.__version__,
        "validate_path": "services.ontology.core.shacl.validate(advanced=True, inference='none')",
        "token_counter": settings.token_counter,
        "vllm_model": settings.vllm_model if settings.token_counter == "vllm" else None,
        "seed": settings.seed,
    }


def _utcnow_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
