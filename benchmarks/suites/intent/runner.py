"""intent suite runner：同金标集三档对照（A0 无约束直觉 / A1 本体约束 / A2=jev 通道）→ 四指标 → results/。

双档定义（docs/Agent/16 §2 intent + 红队审查 E2「ontology 对意图的约束效果从未被数据验证」）：
- **A0 直觉档**：system 提示只给行动类名称清单（无语义标注/无约束条），受评模型凭直觉选；
- **A1 本体约束档**：候选行动类以语义标注注入（action_iri/capability/channel/描述/参数
  schema，经平台 ``render_tool_schema_section`` 同源渲染——services/agent/business/adapters/
  builtin.py:55）+ 候选集封闭约束条（action_catalog.A1_CONSTRAINT_RULES）；
- **A2=jev 通道档**（docs/Agent/17 §1 批次 A，红队审查 E1「GLiNER 装了没接线」的接线后
  实测）：直调 ``services.agent.business.capabilities.jev.engine``（GLiNER 零样本意图路由，
  不走 LLM），输出与 LLM 档同 JSON 契约 → 产出 ``jev_vs_llm`` 差值列。

**近似口径如实声明**（结果 JSON ``a1_mode``/``a2_mode`` 块）：平台 A1 档（OntRAG AgenticRAG
三档装配，docs/OntRAG/AgenticRAG优化方案.md §1——A0 服务端代跑/A1 细粒度工具循环/A2 内核
原生）尚未在意图面落地，本 run 的 A1 为「语义标注增强提示」近似：约束注入面取平台真实语义
标注（ExtensionMeta/action_iri/执行级/描述/schema），非端到端本体推理管线。E2 差值在此口径
下成立，结论外推须带此口径。A2 档同为近似口径：GLiNER 是 span-NER 范式，17 类零样本路由
经「分类前缀+整句 span」（jev-local demo_route 同款），命中 span 可能落在指令前缀噪声
（per_item ``raw_spans_noisy`` 如实标记）——负差值亦是红队攻击性提问的合法答案。

产物：results/intent/<date>/run-<HHMMSS>-<tag>.json（全量指标+环境指纹+逐条明细）与
results/intent/SUMMARY.md（追加式曲线，不覆盖历史——docs/Agent/16 §3）。
"""

from __future__ import annotations

import asyncio
import json
import platform as py_platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from benchmarks.suites.intent import metrics as m
from benchmarks.suites.intent.action_catalog import A1_CONSTRAINT_RULES, ActionClass, build_action_catalog
from benchmarks.suites.intent.config import IntentBenchSettings
from benchmarks.suites.intent.dataset import GoldenItem, load_dataset

ROOT = Path(__file__).resolve().parents[3]  # benchmarks/suites/intent/runner.py → 仓库根

# A1 注入面：平台 H-2 遮蔽式工具 schema 渲染器（KV-cache 前缀稳定纪律同源；builtin.py:55）
# 分位口径复用 rag 套件口径字典（benchmarks 包内共享纯函数，禁本地抄写分位算法）
from benchmarks.suites.rag.metrics import percentile_nearest_rank  # noqa: E402
from services.agent.business.adapters.builtin import render_tool_schema_section  # noqa: E402

_OUTPUT_CONTRACT = (
    '只输出一个 JSON 对象：{"action": "<行动类名称或 out_of_scope>", "confidence": <0~1>，'
    "不要输出 JSON 以外的任何文字。"
)
_SUMMARY_HEADER = (
    "| 时间 | tag | 档 | intent_acc | acc(clear) | acc(amb) | 澄清触发 | 越界拒 | parse_err | tok/条 | 结果文件 |"
)
_SUMMARY_SEPARATOR = (
    "| ---- | --- | --- | ---------- | ---------- | -------- | -------- | ------ | --------- | ------ | -------- |"
)


# ---------------------------------------------------------------- 提示构造（确定性渲染）


def _a0_system_prompt(catalog: tuple[ActionClass, ...]) -> str:
    """A0 直觉档：行动类名称清单（sorted 字节稳定），无语义标注无约束条。"""
    names = "\n".join(f"- {e.name}" for e in catalog)  # catalog 已按 name 排序
    return (
        "你是 ontology-agent 平台的意图路由器。下面是平台行动类名称清单，"
        "对每条用户请求选出最匹配的行动类名称；清单中无合适行动类时输出 \"out_of_scope\"。\n"
        f"{_OUTPUT_CONTRACT}\n"
        "信息是否足以行动由你自行判断。\n\n"
        f"【行动类清单】\n{names}"
    )


def _a1_annotation_definitions(catalog: tuple[ActionClass, ...]) -> dict[str, dict[str, Any]]:
    """A1 语义标注定义表（name → 标注字典；喂给平台 render_tool_schema_section 渲染）。"""
    return {
        e.name: {
            "action_iri": e.action_iri,
            "execution_mode": e.execution_mode,
            "capability": e.capability,
            "channel": e.channel,
            "description": e.description,
            "parameter_schema": e.parameter_schema,
        }
        for e in catalog
    }


def _a1_system_prompt(catalog: tuple[ActionClass, ...]) -> str:
    """A1 本体约束档：候选集封闭约束条 + 语义标注常驻段（平台渲染器，sorted+sort_keys 字节稳定）。"""
    section = render_tool_schema_section(_a1_annotation_definitions(catalog))
    return (
        "你是 ontology-agent 平台的意图路由器，受本体行动类约束。\n"
        f"{A1_CONSTRAINT_RULES}\n"
        f"{_OUTPUT_CONTRACT}\n\n"
        f"{section}"
    )


def _user_prompt(query: str) -> str:
    return f"用户请求：{query}"


def annotation_provenance(catalog: tuple[ActionClass, ...]) -> list[dict[str, Any]]:
    """逐类标注来源落档（平台原文 vs bench 侧补注可辨——A1 口径可审）。"""
    return [
        {
            "name": e.name,
            "action_iri": e.action_iri,
            "execution_mode": e.execution_mode,
            "source_module": e.source_module,
            "description_is_gloss": e.gloss,
        }
        for e in catalog
    ]


# ---------------------------------------------------------------- vLLM 客户端（受评面）


class VllmChatClient:
    """本地 vLLM OpenAI 兼容 /v1/chat/completions 客户端（并发受 settings 管控；保序回填）。"""

    def __init__(self, settings: IntentBenchSettings) -> None:
        self._settings = settings
        self._client: httpx.AsyncClient | None = None
        self._semaphore: asyncio.Semaphore | None = None

    async def __aenter__(self) -> VllmChatClient:
        self._client = httpx.AsyncClient(timeout=self._settings.request_timeout_s)
        self._semaphore = asyncio.Semaphore(self._settings.request_concurrency)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def probe_model(self) -> dict[str, Any]:
        """/v1/models 探活（served id 与模型文件进环境指纹；不可达→RuntimeError 不静默跑分）。"""
        assert self._client is not None
        resp = await self._client.get(f"{self._settings.vllm_base_url.rstrip('/')}/v1/models")
        if resp.status_code != 200:
            raise RuntimeError(f"vLLM /v1/models 探活失败：HTTP {resp.status_code}")
        data = resp.json().get("data") or []
        if not data:
            raise RuntimeError("vLLM /v1/models 无已服务模型")
        first = data[0]
        return {
            "served_id": first.get("id"),
            "model_root": first.get("root"),
            "max_model_len": first.get("max_model_len"),
        }

    async def chat(self, system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        """单次生成：返回 (content, prompt_tokens, completion_tokens)；HTTP/协议错误抛 RuntimeError。"""
        assert self._client is not None and self._semaphore is not None
        async with self._semaphore:
            resp = await self._client.post(
                f"{self._settings.vllm_base_url.rstrip('/')}/v1/chat/completions",
                json={
                    "model": self._settings.vllm_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": self._settings.temperature,
                    "max_tokens": self._settings.max_tokens,
                },
            )
        if resp.status_code != 200:
            raise RuntimeError(f"vLLM chat 失败：HTTP {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        choice = (body.get("choices") or [{}])[0]
        content = str((choice.get("message") or {}).get("content") or "")
        usage = body.get("usage") or {}
        return content, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


# ---------------------------------------------------------------- 单档跑分


async def run_tier(
    tier: str,
    client: VllmChatClient,
    system_prompt: str,
    items: list[GoldenItem],
    settings: IntentBenchSettings,
) -> list[m.ItemVerdict]:
    """单档逐条跑分（并发受控、数据集序回填；单条网络失败记 parse_error 行不炸全程）。"""
    latencies: list[float] = [0.0] * len(items)
    raws: list[str] = [""] * len(items)
    tokens: list[tuple[int, int]] = [(0, 0)] * len(items)

    async def _one(idx: int, item: GoldenItem) -> None:
        t0 = time.perf_counter()
        try:
            content, p_tok, c_tok = await client.chat(system_prompt, _user_prompt(item.query))
        except (httpx.HTTPError, RuntimeError):
            raws[idx] = ""  # 调用失败=无输出：按 parse_error 计入分母（口径见 metrics 模块头）
            return
        latencies[idx] = (time.perf_counter() - t0) * 1000
        raws[idx] = content
        tokens[idx] = (p_tok, c_tok)

    await asyncio.gather(*(_one(i, it) for i, it in enumerate(items)))
    return [
        m.score_item(
            item_id=item.id,
            ambiguity_level=item.ambiguity_level,
            expectation=item.expectation,
            expected_action=item.expected_action,
            raw_output=raws[idx],
            confidence_floor=settings.out_of_scope_confidence_floor,
            latency_ms=latencies[idx],
            prompt_tokens=tokens[idx][0],
            completion_tokens=tokens[idx][1],
        )
        for idx, item in enumerate(items)
    ]


# ---------------------------------------------------------------- A2 档（jev 通道，不走 LLM）


def _a2_mode_block(engine_info: dict[str, Any], catalog_count: int) -> dict[str, Any]:
    """A2=jev 通道口径声明块（与 a1_mode 同纪律：近似口径如实入档，不冒充端到端本体约束）。"""
    return {
        "kind": "gliner_zero_shot_routing",
        "approximation": True,
        "note": (
            "A2=jev 通道（docs/Agent/17 §1 批次 A；红队 E1「GLiNER 装了没接线」接线后实测）："
            "直调 capabilities.jev.engine（GLiNER gliner_multi 零样本意图路由，不走 LLM）。"
            "口径边界：GLiNER 是 span-NER 范式，17 类路由经「分类前缀+整句 span」近似"
            "（jev-local demo_route 同款），命中 span 可能落在指令前缀噪声（per_item "
            "raw_spans_noisy 如实标记）——本档数字为该口径下的实测，负差值亦是答案。"
            "延迟口径：A2=纯判定延迟（逐条串行直调），A0/A1=含并发排队（既有口径），"
            "latency 差值列解读须带此口径。"
        ),
        "engine": engine_info,
        "labels_source": "services.agent.business.capabilities.jev.labels（platform 17 行动类常量生成）",
        "candidate_classes": catalog_count,
        "llm_involved": False,
    }


async def run_jev_tier(
    items: list[GoldenItem],
    engine: Any,
    settings: IntentBenchSettings,
) -> tuple[list[m.ItemVerdict], list[dict[str, Any]]]:
    """A2 档逐条跑分：直调 jev 引擎（逐条串行直调——引擎内锁本就串行推理，并发 gather
    只会制造排队等待并撞 10s 超时钳制的假 parse_error，2026-10-07 首跑实测 87/100 假超时
    后改定）。延迟口径=纯判定延迟（无排队项，与 LLM 档「含并发排队」口径不同，块内注明）。

    判定输出转与 LLM 档同构的 JSON 契约（{"action","confidence",...}）→ 复用 metrics.
    score_item 同口径判分（parse_error 分母语义一致）；判定失败/超时=无输出按 parse_error
    计入分母（与 LLM 档调用失败同口径，不静默挽救）。返回 (verdicts, detections 明细)。
    """
    from services.agent.business.capabilities.jev.engine import JevUnavailableError

    verdicts: list[m.ItemVerdict] = []
    detections: list[dict[str, Any]] = []
    for item in items:
        t0 = time.perf_counter()
        try:
            detection = await engine.detect(item.query)
        except (JevUnavailableError, TimeoutError):  # asyncio.TimeoutError=TimeoutError 别名（py3.11+）
            # 引擎不可用/超时=无输出：按 parse_error 计入分母（口径同 LLM 档网络失败）
            verdicts.append(
                m.score_item(
                    item_id=item.id,
                    ambiguity_level=item.ambiguity_level,
                    expectation=item.expectation,
                    expected_action=item.expected_action,
                    raw_output="",
                    confidence_floor=settings.out_of_scope_confidence_floor,
                )
            )
            detections.append({})
            continue
        latency_ms = (time.perf_counter() - t0) * 1000
        detections.append(
            {
                "item_id": item.id,
                "expected_action": item.expected_action,
                "action": detection.action,
                "confidence": detection.confidence,
                "entities": detection.entities,
                "intents": detection.intents,
                "raw_spans_noisy": detection.raw_spans_noisy,
                "latency_ms": round(latency_ms, 2),
            }
        )
        # 与 LLM 档同构 JSON 契约（score_item.parse_prediction 可解；附加键解析侧忽略）
        raw = json.dumps(
            {
                "action": detection.action,
                "confidence": detection.confidence,
                "raw_spans_noisy": detection.raw_spans_noisy,
                "n_entities": len(detection.entities),
                "n_intents": len(detection.intents),
            },
            ensure_ascii=False,
        )
        verdicts.append(
            m.score_item(
                item_id=item.id,
                ambiguity_level=item.ambiguity_level,
                expectation=item.expectation,
                expected_action=item.expected_action,
                raw_output=raw,
                confidence_floor=settings.out_of_scope_confidence_floor,
                latency_ms=latency_ms,
            )
        )
    return verdicts, detections


# ---------------------------------------------------------------- 环境指纹与产物


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _a1_mode_block(catalog: tuple[ActionClass, ...]) -> dict[str, Any]:
    """A1 近似口径声明块（README 已知边界同纪律：近似须如实入档，不冒充端到端本体约束）。"""
    return {
        "kind": "semantic_annotation_enhanced_prompt",
        "approximation": True,
        "note": (
            "平台 A1 档（OntRAG AgenticRAG 三档装配：A0 服务端代跑/A1 细粒度工具循环/A2 内核原生）"
            "尚未在意图面落地——本 run 以「行动类语义标注增强提示」作 A1 近似：候选集经平台真实"
            "语义标注（action_iri/执行级/capability/描述/参数 schema）+ 候选集封闭约束条注入；"
            "E2 差值在此口径下成立，外推须带此口径。"
        ),
        "injection_point": "services.agent.business.adapters.builtin.render_tool_schema_section",
        "constraint_rules": "benchmarks.suites.intent.action_catalog.A1_CONSTRAINT_RULES",
        "candidate_classes": len(catalog),
        "annotation_provenance": annotation_provenance(catalog),
    }


def write_results(
    settings: IntentBenchSettings, result: dict[str, Any], tiers: dict[str, m.IntentMetrics]
) -> tuple[Path, Path]:
    """结果 JSON 落 results/intent/<date>/ + SUMMARY.md 追加一行（不覆盖历史）。"""
    now = datetime.now()
    run_dir = ROOT / settings.results_dir / settings.suite_name / now.strftime("%Y-%m-%d")
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / f"run-{now.strftime('%H%M%S')}-{settings.tag}.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    summary_path = ROOT / settings.results_dir / settings.suite_name / "SUMMARY.md"
    lines: list[str] = []
    if not summary_path.exists():
        lines.append("# intent suite 结果曲线（SUMMARY.md，机器写人审；追加式不覆盖历史）\n")
        lines.append(_SUMMARY_HEADER)
        lines.append(_SUMMARY_SEPARATOR)
    for name in ("a0", "a1", "a2"):
        if name not in tiers:
            continue  # A2 显式跳过（jev_tier_enabled=False）时不落行，历史曲线不掺空档
        mt = tiers[name]
        mt = tiers[name]
        lines.append(
            f"| {now.strftime('%Y-%m-%d %H:%M:%S')} | {settings.tag} | {name} | {mt.intent_accuracy:.3f} "
            f"| {mt.intent_accuracy_clear:.3f} | {mt.intent_accuracy_ambiguous:.3f} "
            f"| {mt.clarification_trigger_rate:.3f} | {mt.out_of_scope_reject_rate:.3f} "
            f"| {mt.parse_error_count} | {mt.cost_per_item_tokens:.0f} | {json_path.name} |"
        )
    with summary_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return json_path, summary_path


def _per_item_rows(verdicts: list[m.ItemVerdict]) -> list[dict[str, Any]]:
    return [
        {
            "item_id": v.item_id,
            "ambiguity_level": v.ambiguity_level,
            "expectation": v.expectation,
            "expected_action": v.expected_action,
            "predicted_action": v.predicted_action,
            "confidence": v.confidence,
            "parse_error": v.parse_error,
            "correct": v.correct,
            "clarified": v.clarified,
            "rejected": v.rejected,
            "latency_ms": round(v.latency_ms, 2),
            "tokens": {"prompt": v.prompt_tokens, "completion": v.completion_tokens},
            "raw_output": v.raw_output[:300],
        }
        for v in verdicts
    ]


# ---------------------------------------------------------------- 主流程


async def run_suite(
    settings: IntentBenchSettings,
    *,
    smoke: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """全链：金标装载 → 目录构造 → 三档同集跑分（A0/A1 LLM + A2 jev）→ 四指标+增益 → 落盘。

    返回结果 dict（与落盘 JSON 同构）；供 run.py CLI 与测试直接调用。
    win32 事件循环策略由入口 run.py 在 asyncio.run 前设置（纯 httpx 无 psycopg 依赖，双策略皆可）。
    A2 档（jev 通道）默认开（IntentBenchSettings.jev_tier_enabled）——jev 引擎不可用
    （依赖缺失）时如实 RuntimeError（fail loud，与 vLLM 探活同纪律，不静默跑假分）。
    """
    full_items = load_dataset(settings.dataset_version)
    items = full_items[:limit] if limit is not None else full_items
    catalog = build_action_catalog()

    async with VllmChatClient(settings) as client:
        model_info = await client.probe_model()
        verdicts_a0 = await run_tier("a0", client, _a0_system_prompt(catalog), items, settings)
        verdicts_a1 = await run_tier("a1", client, _a1_system_prompt(catalog), items, settings)

    lat_a0 = percentile_nearest_rank((v.latency_ms for v in verdicts_a0), 50)
    lat_a1 = percentile_nearest_rank((v.latency_ms for v in verdicts_a1), 50)
    metrics_a0 = m.aggregate(verdicts_a0, latency_p50_ms=lat_a0)
    metrics_a1 = m.aggregate(verdicts_a1, latency_p50_ms=lat_a1)
    gain = m.ontology_constraint_gain(metrics_a0, metrics_a1)

    # A2=jev 档（E1 闭环）：引擎不可用=如实失败（fail loud）；False=显式跳过（指标置 None）
    metrics_a2: m.IntentMetrics | None = None
    detections: list[dict[str, Any]] = []
    a2_mode: dict[str, Any] | None = None
    if settings.jev_tier_enabled:
        from services.agent.business.capabilities.jev.engine import get_jev_engine

        engine = get_jev_engine()  # 引擎参数（threshold/timeout/model）全走平台 Settings（OA_JEV_*）
        engine_info = engine.warmup()  # 依赖缺失/模型加载失败 → JevUnavailableError 逸出（不静默跑分）
        verdicts_a2, detections = await run_jev_tier(items, engine, settings)
        lat_a2 = percentile_nearest_rank((v.latency_ms for v in verdicts_a2), 50)
        metrics_a2 = m.aggregate(verdicts_a2, latency_p50_ms=lat_a2)
        a2_mode = _a2_mode_block(
            {**engine_info, "threshold": engine.threshold, "timeout_s": engine.timeout_s}, len(catalog)
        )

    from benchmarks.suites.intent.datasets.build_golden_v0 import dataset_sha256

    result: dict[str, Any] = {
        "suite": settings.suite_name,
        "scenario": "intent_dual_tier",  # orsi_link.py 场景键（suite=intent → ORSI face O3；A2 为同场景第三档）
        "tag": settings.tag,
        "mode": "smoke" if smoke else "run",
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "env": {
            "commit": git_commit(),
            "python": sys.version.split()[0],
            "platform": py_platform.platform(),
            "model": {"base_url": settings.vllm_base_url, **model_info},
            "sampling": {"temperature": settings.temperature, "max_tokens": settings.max_tokens},
        },
        "config": {
            "dataset_version": settings.dataset_version,
            "items": len(items),
            "out_of_scope_confidence_floor": settings.out_of_scope_confidence_floor,
            "request_concurrency": settings.request_concurrency,
            "jev_tier_enabled": settings.jev_tier_enabled,
        },
        "dataset": {
            "version": settings.dataset_version,
            "sha256": dataset_sha256([_item_to_dict(it) for it in full_items]),  # 全集指纹（limit 只截跑分规模）
            "distribution": _distribution(items),
        },
        "a1_mode": _a1_mode_block(catalog),
        "a2_mode": a2_mode,  # None=显式跳过（jev_tier_enabled=False）
        "metrics": _flat_metrics(metrics_a0, metrics_a1, metrics_a2),  # 顶层平铺（orsi_link evidence 读数）
        "tiers": {
            "a0": {"metrics": metrics_a0.to_dict(), "per_item": _per_item_rows(metrics_a0.per_item)},
            "a1": {"metrics": metrics_a1.to_dict(), "per_item": _per_item_rows(metrics_a1.per_item)},
            **(
                {"a2": {"metrics": metrics_a2.to_dict(), "per_item": _per_item_rows(metrics_a2.per_item)}}
                if metrics_a2 is not None
                else {}
            ),
        },
        "ontology_constraint_gain": gain,
        "jev_vs_llm": _jev_vs_llm_block(metrics_a0, metrics_a1, metrics_a2),  # E1 数据答案（明细块）
        "jev_detections": detections,  # A2 全量判定明细（E3 融合语义后续批次的直接输入）
    }
    tiers_for_summary = {"a0": metrics_a0, "a1": metrics_a1}
    if metrics_a2 is not None:
        tiers_for_summary["a2"] = metrics_a2
    json_path, summary_path = write_results(settings, result, tiers_for_summary)
    result["artifacts"] = {
        "result_json": str(json_path.relative_to(ROOT)),
        "summary_md": str(summary_path.relative_to(ROOT)),
    }
    return result


def _flat_metrics(
    metrics_a0: m.IntentMetrics,
    metrics_a1: m.IntentMetrics,
    metrics_a2: m.IntentMetrics | None = None,
) -> dict[str, Any]:
    """顶层平铺指标块（orsi_link.py 只读平铺数值键；双档四指标 10 键 + A2 三键，键名自说明）。

    A2 三键（docs/Agent/17 §1，E1 数据答案）：``jev_accuracy``（A2 意图准确率）、
    ``jev_vs_llm_gain``（A2−A1 意图准确率差，负值如实保留）、``jev_latency_vs_llm``
    （A2−A1 p50 延迟差 ms，负=更快）。A2 档跳过时三键置 None（orsi_link 数值键过滤掉非数）。
    """
    flat: dict[str, Any] = {
        "a0_intent_accuracy": metrics_a0.intent_accuracy,
        "a0_clarification_trigger_rate": metrics_a0.clarification_trigger_rate,
        "a0_out_of_scope_reject_rate": metrics_a0.out_of_scope_reject_rate,
        "a1_intent_accuracy": metrics_a1.intent_accuracy,
        "a1_clarification_trigger_rate": metrics_a1.clarification_trigger_rate,
        "a1_out_of_scope_reject_rate": metrics_a1.out_of_scope_reject_rate,
        "gain_intent_accuracy": metrics_a1.intent_accuracy - metrics_a0.intent_accuracy,
        "gain_clarification_trigger_rate": metrics_a1.clarification_trigger_rate
        - metrics_a0.clarification_trigger_rate,
        "gain_out_of_scope_reject_rate": metrics_a1.out_of_scope_reject_rate - metrics_a0.out_of_scope_reject_rate,
        "gain_parse_error_count": metrics_a1.parse_error_count - metrics_a0.parse_error_count,
    }
    if metrics_a2 is not None:
        flat["jev_accuracy"] = metrics_a2.intent_accuracy
        flat["jev_vs_llm_gain"] = metrics_a2.intent_accuracy - metrics_a1.intent_accuracy  # 对照=A1 现役本体约束档
        flat["jev_latency_vs_llm"] = metrics_a2.latency_p50_ms - metrics_a1.latency_p50_ms
    else:
        flat["jev_accuracy"] = None
        flat["jev_vs_llm_gain"] = None
        flat["jev_latency_vs_llm"] = None
    return flat


def _jev_vs_llm_block(
    metrics_a0: m.IntentMetrics,
    metrics_a1: m.IntentMetrics,
    metrics_a2: m.IntentMetrics | None,
) -> dict[str, Any] | None:
    """E1 数据答案明细块：A2=jev 通道 vs LLM 双档的逐指标差值（差值列，负值如实保留）。"""
    if metrics_a2 is None:
        return None
    diff = {
        "reference": "a1",
        "reference_note": "对照缺省=A1（本体约束档，现役 LLM 最优意图档）；vs_a0 供双基线审阅",
        "intent_accuracy": metrics_a2.intent_accuracy - metrics_a1.intent_accuracy,
        "intent_accuracy_clear": metrics_a2.intent_accuracy_clear - metrics_a1.intent_accuracy_clear,
        "intent_accuracy_ambiguous": metrics_a2.intent_accuracy_ambiguous - metrics_a1.intent_accuracy_ambiguous,
        "clarification_trigger_rate": metrics_a2.clarification_trigger_rate - metrics_a1.clarification_trigger_rate,
        "out_of_scope_reject_rate": metrics_a2.out_of_scope_reject_rate - metrics_a1.out_of_scope_reject_rate,
        "parse_error_count": metrics_a2.parse_error_count - metrics_a1.parse_error_count,
        "latency_p50_ms": metrics_a2.latency_p50_ms - metrics_a1.latency_p50_ms,
        "cost_per_item_tokens": metrics_a2.cost_per_item_tokens - metrics_a1.cost_per_item_tokens,
    }
    vs_a0 = {
        "intent_accuracy": metrics_a2.intent_accuracy - metrics_a0.intent_accuracy,
        "clarification_trigger_rate": metrics_a2.clarification_trigger_rate - metrics_a0.clarification_trigger_rate,
        "out_of_scope_reject_rate": metrics_a2.out_of_scope_reject_rate - metrics_a0.out_of_scope_reject_rate,
        "latency_p50_ms": metrics_a2.latency_p50_ms - metrics_a0.latency_p50_ms,
    }
    return {
        "definition": "A2（jev 通道，GLiNER 零样本路由）− LLM 档（A1 本体约束/A0 直觉）逐指标差值",
        "diff": diff,
        "vs_a0": vs_a0,
        "latency_p50_ms": {
            "jev": metrics_a2.latency_p50_ms,
            "a0": metrics_a0.latency_p50_ms,
            "a1": metrics_a1.latency_p50_ms,
        },
    }


def _distribution(items: list[GoldenItem]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for it in items:
        dist[it.ambiguity_level] = dist.get(it.ambiguity_level, 0) + 1
    return dict(sorted(dist.items()))


def _item_to_dict(it: GoldenItem) -> dict[str, Any]:
    return {
        "id": it.id,
        "query": it.query,
        "expected_action": it.expected_action,
        "ambiguity_level": it.ambiguity_level,
        "notes": it.notes,
        "expectation": it.expectation,
    }
