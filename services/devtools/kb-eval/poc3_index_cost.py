#!/usr/bin/env python3
"""PoC3 索引成本标定（锚点 docs/architecture/01 §7 PoC③；OntRAG 知识库GraphRAG设计 §2/§3/§4.0）。

对 services/seeds/samples/power/ 实测三件事，并按 §3 公式外推完整 GraphRAG 成本：
  1. chunk_document 语义分块统计：chunk 数 / token 总量（token 口径 = len(text)//2 中文近似，
     与 services.semantic.knowledge.chunking.estimate_tokens 同源，随本 PoC 冻结）；
  2. bge-m3 实测嵌入吞吐（POST {ollama}/api/embed，批量 32，记录 tokens/s 与总耗时；
     Ollama 不可达则标注「运行时未达，估算」，不阻塞）；
  3. 完整 GraphRAG 成本外推：每 chunk 1 次实体抽取 LLM 调用 + Leiden 社区数 × 1 次社区摘要调用；
     用可载入的 qwen 模型（优先 qwen3:8b，内存不足降级 qwen3:0.6b）实测 3 次真实抽取调用取
     单次延迟/token 均值再外推——报告必须区分【实测】与【外推/估算】。

输出：stdout markdown 对比表 + services/devtools/kb-eval/poc3_results.json。

用法（仓库根目录）：
  python services/devtools/kb-eval/poc3_index_cost.py            # 全量实测 + 外推
  python services/devtools/kb-eval/poc3_index_cost.py --skip-llm # 跳过 LLM 实测（仅分块+嵌入）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:  # 工具层脚本：引导仓库根以复用 services.*
    sys.path.insert(0, str(REPO_ROOT))

CORPUS_DIR: Final = REPO_ROOT / "services" / "seeds" / "samples" / "power"
RESULT_PATH: Final = Path(__file__).resolve().parent / "poc3_results.json"

EMBED_MODEL: Final = "bge-m3"
EMBED_BATCH: Final = 32
EMBED_TIMEOUT_S: Final = 180.0
LLM_CANDIDATES: Final = ["qwen3:8b", "qwen3:0.6b"]  # 优先 8b；载入失败（内存不足）自动降级
LLM_CALLS: Final = 3  # 实测抽取调用次数（任务口径 2~3 次）
EXTRACT_TIMEOUT_S: Final = 300.0
# 外推参数（全部为估算假设，报告标注）
ENTITIES_PER_CHUNK: Final = 6  # 每 chunk 抽出实体数假设（电力台账类语料经验值）
COMMUNITY_DIVIDERS: Final = (10, 40)  # L0≈E/10、L1≈E/40，L2 固定 ≤3（层次社区经验比例）
SUMMARY_PROMPT_TOKENS: Final = 800  # 单社区摘要输入 token 假设
SUMMARY_COMPLETION_TOKENS: Final = 400  # 单社区摘要输出 token 假设
ENTITY_JSON_BYTES_PER_CHUNK: Final = 2048  # 实体抽取结果落存储估算
REPORT_BYTES_PER_COMMUNITY: Final = 3072  # community report 落存储估算

_CHUNK_BUDGET_NOTE: Final = "分块目标 512±128 token（§2.1）"
# 注：_CHUNK_BUDGET_NOTE 仅作文档说明；提示词改为 _extract_prompt 构造式生成。


def _extract_prompt(chunk_text: str) -> str:
    """构造抽取提示词（避免多行长字符串触发 lint）。"""
    types = "Feeder|Transformer|Switch|LineSection|Customer|OutageOrder|OutageEvent|RepairCrew"
    preds = "inSection|servesCustomer|dispatchedTo|affectsFeeder"
    return (
        "你是电力领域知识抽取器。从以下文本中抽取实体与关系，只输出 JSON：\n"
        f'{{"entities": [{{"name": "...", "type": "{types}"}}], '
        f'"relations": [{{"subject": "...", "predicate": "{preds}", "object": "..."}}]}}\n'
        "禁止凭空创造文本中没有的实体。文本：\n" + chunk_text
    )


def _load_ollama_base_url() -> str:
    """优先复用 services.infra.config（OA_ 前缀统一配置层），失败回落环境变量/默认值。"""
    try:
        from services.infra.config import get_settings

        return str(get_settings().ollama_base_url)
    except Exception:  # noqa: BLE001 工具脚本容错：任何导入/加载失败都回落
        return os.getenv("OA_OLLAMA_BASE_URL", "http://localhost:11434")


def load_corpus() -> list[tuple[str, str]]:
    """读样例语料（排除 MANIFEST.md——目录元数据，非业务语料）。"""
    docs = []
    for path in sorted(CORPUS_DIR.glob("*.md")):
        if path.name == "MANIFEST.md":
            continue
        docs.append((path.stem, path.read_text(encoding="utf-8")))
    if not docs:
        raise SystemExit(f"语料目录为空：{CORPUS_DIR}")
    return docs


# ---------------------------------------------------------------------------
# 阶段 1：语义分块统计（复用 services chunk_document）
# ---------------------------------------------------------------------------


def phase_chunking(docs: list[tuple[str, str]]) -> dict:
    try:
        from services.semantic.knowledge.chunking import chunk_document
    except ImportError as exc:
        raise SystemExit(f"无法导入 services.semantic.knowledge.chunking（前置：仓库根可导入）: {exc}") from exc

    per_doc, all_chunks = [], []
    for stem, text in docs:
        chunks = chunk_document(text)
        per_doc.append({"doc_id": stem, "chars": len(text), "chunks": len(chunks),
                        "tokens": sum(c.token_count for c in chunks)})
        all_chunks.extend(chunks)
    sizes = [c.token_count for c in all_chunks]
    total_tokens = sum(sizes)
    return {
        "docs": len(docs),
        "chunks_total": len(all_chunks),
        "chars_total": sum(p["chars"] for p in per_doc),
        "tokens_total_est": total_tokens,  # 口径 = len//2
        "chunk_tokens_min": min(sizes),
        "chunk_tokens_median": statistics.median(sizes),
        "chunk_tokens_max": max(sizes),
        "chunks_oversized": sum(1 for c in all_chunks if c.meta.get("oversized")),
        "chunks_table_rowgroup": sum(1 for c in all_chunks if c.meta.get("row_group")),
        "per_doc": per_doc,
        "note": "token 口径 = len(text)//2（中文近似，chunking.estimate_tokens 同源）",
    }


# ---------------------------------------------------------------------------
# 阶段 2：bge-m3 嵌入吞吐（复用 services OllamaEmbedder；不可达 → 估算不阻塞）
# ---------------------------------------------------------------------------


async def _embed_all(base_url: str, texts: list[str]) -> tuple[float, int, list[list[float]]]:
    from services.semantic.knowledge.embed import OllamaEmbedder

    embedder = OllamaEmbedder(base_url, model=EMBED_MODEL, timeout=EMBED_TIMEOUT_S, batch_size=EMBED_BATCH)
    t0 = time.perf_counter()
    vectors = await embedder.embed(texts)
    elapsed = time.perf_counter() - t0
    await embedder.aclose()
    return elapsed, len(vectors[0]) if vectors else 0, vectors


async def phase_embedding(base_url: str, chunk_texts: list[str], est_tokens_total: int) -> dict:
    try:
        elapsed, dim, vectors = await _embed_all(base_url, chunk_texts)
    except Exception as exc:  # EmbeddingUnavailableError / ImportError 一律降级为估算
        est_seconds = round(est_tokens_total / 1200.0, 1)  # 估算假设：bge-m3 CPU ≈1200 tokens/s
        return {
            "status": "运行时未达，估算",
            "error": f"{type(exc).__name__}: {exc}",
            "model": EMBED_MODEL,
            "batch_size": EMBED_BATCH,
            "est_tokens_total": est_tokens_total,
            "estimated_seconds": est_seconds,
            "estimated_tokens_per_s": round(est_tokens_total / est_seconds, 1) if est_seconds else 0.0,
            "note": "吞吐假设 1200 tokens/s（CPU 档估算值），报告必须标注为估算",
        }
    return {
        "status": "实测",
        "model": EMBED_MODEL,
        "batch_size": EMBED_BATCH,
        "chunks": len(chunk_texts),
        "est_tokens_total": est_tokens_total,
        "wall_seconds": round(elapsed, 2),
        "est_tokens_per_s": round(est_tokens_total / elapsed, 1) if elapsed else 0.0,
        "chunks_per_s": round(len(chunk_texts) / elapsed, 2) if elapsed else 0.0,
        "dim": dim,
        "storage_mb_all_chunks": round(len(vectors) * dim * 4 / 1024 / 1024, 2),  # float32
    }


# ---------------------------------------------------------------------------
# 阶段 3：qwen 实体抽取实测（模型自动降级）与完整 GraphRAG 成本外推
# ---------------------------------------------------------------------------


async def _chat_once(base_url: str, model: str, prompt: str) -> dict:
    """单次 /api/chat（think 关闭；旧版不支持 think 字段则原样重试）。"""
    import httpx

    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "stream": False, "think": False}
    async with httpx.AsyncClient(timeout=EXTRACT_TIMEOUT_S) as client:
        resp = await client.post(f"{base_url}/api/chat", json=payload)
        if resp.status_code == 400 and "think" in resp.text.lower():
            payload.pop("think", None)
            resp = await client.post(f"{base_url}/api/chat", json=payload)
        data = resp.json()
    if "error" in data:
        raise RuntimeError(f"模型 {model} 调用失败: {data['error']}")
    return data


async def phase_llm_extraction(base_url: str, sample_texts: list[str]) -> dict:
    """选可用模型 → 3 次真实抽取调用取均值 → 返回实测样本统计。"""
    chosen, choice_note = None, ""
    for model in LLM_CANDIDATES:
        try:
            await _chat_once(base_url, model, "只输出JSON: {\"ok\": true}")
            chosen = model
            break
        except Exception as exc:  # noqa: BLE001 逐个候选探测
            choice_note += f"{model}: {exc}; "
    if chosen is None:
        return {"status": "运行时未达，估算", "error": choice_note or "无可用模型",
                "note": "外推改用假设值：单次抽取 8s / 输入 900 tok / 输出 400 tok（必须标注为估算）",
                "assumed_call_seconds": 8.0, "assumed_prompt_tokens": 900, "assumed_completion_tokens": 400}

    samples = []
    for i, text in enumerate(sample_texts[:LLM_CALLS], 1):
        prompt = _extract_prompt(text)
        t0 = time.perf_counter()
        data = await _chat_once(base_url, chosen, prompt)
        wall = time.perf_counter() - t0
        samples.append({
            "call": i, "wall_seconds": round(wall, 2),
            "prompt_tokens": data.get("prompt_eval_count"),
            "completion_tokens": data.get("eval_count"),
            "content_head": (data.get("message", {}).get("content", "") or "")[:120],
        })
    prompt_tokens = [s["prompt_tokens"] or 0 for s in samples]
    completion_tokens = [s["completion_tokens"] or 0 for s in samples]
    walls = [s["wall_seconds"] for s in samples]
    return {
        "status": "实测",
        "model": chosen,
        "candidate_probes": choice_note,
        "calls": len(samples),
        "samples": samples,
        "call_seconds_avg": round(statistics.mean(walls), 2),
        "prompt_tokens_avg": round(statistics.mean(prompt_tokens), 1),
        "completion_tokens_avg": round(statistics.mean(completion_tokens), 1),
    }


def extrapolate_full_graphrag(chunks: int, llm: dict) -> dict:
    """完整 GraphRAG 成本外推（OntRAG §3 四阶段公式；实体数/社区数为估算假设）。"""
    measured = llm.get("status") == "实测"
    call_s = llm.get("call_seconds_avg") if measured else llm.get("assumed_call_seconds", 8.0)
    prompt_t = llm.get("prompt_tokens_avg") if measured else llm.get("assumed_prompt_tokens", 900)
    completion_t = llm.get("completion_tokens_avg") if measured else llm.get("assumed_completion_tokens", 400)

    entities = chunks * ENTITIES_PER_CHUNK
    l0 = max(3, round(entities / COMMUNITY_DIVIDERS[0]))
    l1 = max(1, round(entities / COMMUNITY_DIVIDERS[1]))
    l2 = min(3, max(1, entities // 200))
    communities = l0 + l1 + l2

    extraction_calls = chunks  # 每 chunk 1 次实体抽取（§3 阶段 1 口径）
    summary_calls = communities  # 每社区 1 次摘要（§3 阶段 3 口径）
    extraction_tokens = extraction_calls * (prompt_t + completion_t)
    summary_tokens = summary_calls * (SUMMARY_PROMPT_TOKENS + SUMMARY_COMPLETION_TOKENS)
    # 摘要调用输入更长、生成更慢：耗时按抽取调用 1.5x 系数折算（估算）
    extraction_seconds = extraction_calls * call_s
    summary_seconds = summary_calls * call_s * 1.5
    extra_bytes = chunks * ENTITY_JSON_BYTES_PER_CHUNK + communities * REPORT_BYTES_PER_COMMUNITY
    storage_extra_mb = round(extra_bytes / 1024 / 1024, 2)
    return {
        "assumption_entities_per_chunk": ENTITIES_PER_CHUNK,
        "estimated_entities": entities,
        "communities_l0_l1_l2": [l0, l1, l2],
        "communities_total": communities,
        "extraction_calls": extraction_calls,
        "summary_calls": summary_calls,
        "llm_calls_total": extraction_calls + summary_calls,
        "llm_tokens_total_est": extraction_tokens + summary_tokens,
        "extraction_tokens_est": extraction_tokens,
        "summary_tokens_est": summary_tokens,
        "index_seconds_est": round(extraction_seconds + summary_seconds, 1),
        "storage_extra_mb_est": storage_extra_mb,
        "measured_basis": f"单次抽取实测 {call_s}s / {prompt_t}+{completion_t} tok" if measured
        else "单次抽取为假设值 8s / 900+400 tok（LLM 未达，估算口径）",
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PoC3 索引成本标定（分块/嵌入/完整GraphRAG外推）")
    parser.add_argument("--skip-llm", action="store_true", help="跳过 LLM 抽取实测（仅分块+嵌入）")
    parser.add_argument("--samples", type=int, default=3, help="LLM 抽取实测样本 chunk 数（默认 3）")
    return parser.parse_args()


async def main_async() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    args = _parse_args()
    base_url = _load_ollama_base_url()
    docs = load_corpus()
    print(f"[环境] ollama={base_url} | 语料 {len(docs)} 篇（不含 MANIFEST.md）")

    result: dict = {"generated_at": datetime.now(UTC).isoformat(), "ollama_base_url": base_url}
    chunking = phase_chunking(docs)
    result["chunking"] = chunking
    print(f"[分块] {chunking['chunks_total']} chunks / {chunking['tokens_total_est']:,} est-tokens"
          f"（中位 {chunking['chunk_tokens_median']:.0f}，最大 {chunking['chunk_tokens_max']}）")

    # 重建 chunk 文本清单用于嵌入与抽样（与阶段 1 同口径）
    from services.semantic.knowledge.chunking import chunk_document

    chunk_texts = [c.content for _, text in docs for c in chunk_document(text)]

    embed = await phase_embedding(base_url, chunk_texts, chunking["tokens_total_est"])
    result["embedding"] = embed
    print(f"[嵌入] {embed['status']} | {embed.get('wall_seconds', '-')}s | "
          f"{embed.get('est_tokens_per_s', embed.get('estimated_tokens_per_s'))} est-tokens/s")

    if args.skip_llm:
        llm = {"status": "跳过（--skip-llm）"}
    else:
        step = max(1, len(chunk_texts) // args.samples)
        samples = chunk_texts[::step][: args.samples]
        llm = await phase_llm_extraction(base_url, samples)
        result["llm_extraction_samples"] = llm
        print(f"[抽取实测] {llm['status']} | 模型 {llm.get('model', '-')} | "
              f"均值 {llm.get('call_seconds_avg', '-')}s / {llm.get('prompt_tokens_avg', '-')}+"
              f"{llm.get('completion_tokens_avg', '-')} tok")

    full = extrapolate_full_graphrag(chunking["chunks_total"], llm)
    result["full_graphrag_extrapolation"] = full
    lazy_seconds = embed.get("wall_seconds") or embed.get("estimated_seconds")
    lazy_tokens = embed.get("est_tokens_total")
    comparison = {
        "lazygraphrag": {
            "llm_calls": 0,
            "llm_tokens": 0,
            "embed_tokens_est": lazy_tokens,
            "index_seconds": lazy_seconds,
            "storage_mb": embed.get("storage_mb_all_chunks"),
            "note": "索引期只做 chunks/实体描述向量；图遍历留到查询时（§4.0 默认档）",
        },
        "full_graphrag": {
            "llm_calls": full["llm_calls_total"],
            "llm_tokens_est": full["llm_tokens_total_est"],
            "embed_tokens_est": lazy_tokens,
            "index_seconds_est": round((lazy_seconds or 0) + full["index_seconds_est"], 1),
            "storage_mb_est": round((embed.get("storage_mb_all_chunks") or 0) + full["storage_extra_mb_est"], 2),
            "note": "叠加实体抽取（每 chunk 1 调用）+ Leiden 社区摘要（社区数 × 1 调用）",
        },
        "cost_ratio_full_over_lazy": {
            "llm_calls": full["llm_calls_total"],
            "time_x": round(full["index_seconds_est"] / max(lazy_seconds or 1, 1e-6), 1),
        },
        "per_query_lazy_est": {
            "llm_calls": 1, "llm_tokens_est": 1500,
            "note": "查询时图遍历 + 1 次作答调用（输入含邻域 chunk，估算口径）",
        },
    }
    result["comparison"] = comparison
    result["measurement_notes"] = [
        "token 口径 = len(text)//2（中文近似；嵌入吞吐另可对照 Ollama prompt_eval_count）",
        "实测项：chunk 数量与尺寸、bge-m3 嵌入耗时、qwen 抽取样本调用；",
        "外推项：完整 GraphRAG 的抽取/摘要调用数与总耗时（公式 × 实测单次均值）；",
        "估算项：每 chunk 实体数 6、社区比例 E/10 与 E/40、摘要输入输出 800/400 tok、存储字节数",
        "（以上三项在报告中必须与实测分列，禁止包装成实测）",
    ]

    out = RESULT_PATH
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[落盘] {out}")

    print("\n## LazyGraphRAG vs 完整 GraphRAG（markdown）\n")
    print("| 指标 | LazyGraphRAG（默认档） | 完整 GraphRAG（可选档） |")
    print("| --- | --- | --- |")
    lz, fu = comparison["lazygraphrag"], comparison["full_graphrag"]
    print(f"| LLM 调用次数 | {lz['llm_calls']} | {fu['llm_calls']:,} |")
    print(f"| LLM token | 0 | {fu['llm_tokens_est']:,}（估算） |")
    print(f"| 嵌入 token（est 口径） | {lz['embed_tokens_est']:,} | {fu['embed_tokens_est']:,} |")
    print(f"| 索引耗时 | {lz['index_seconds']}s（{'实测' if embed['status'] == '实测' else '估算'}）"
          f" | {fu['index_seconds_est']}s（外推） |")
    print(f"| 索引新增存储 | {lz['storage_mb']}MB 向量 | {fu['storage_mb_est']}MB（含实体/社区报告，估算） |")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
