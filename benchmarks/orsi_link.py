"""ORSI 挂钩骨架（docs/Agent/16 §3）：suite 结果 JSON → ORSI capability 注册建议。

本波边界（16 篇 §4）：只产出建议 JSON，**不自动注册**——注册仍走 ORSI 注册表端点
（POST /api/v1/orsi/capabilities，services/rsi/api/capabilities.py）人工确认后提交。

机制化要点（16 篇 §3 原文）：
- evidence_uri = results JSON 路径（suite 结果即能力的「参考依据」载体）；
- status = candidate（宪法 3 候选非成品；promoted 恒不可注册直达）；
- capability_fingerprint 关联**场景集哈希**——场景集变=指纹变=能力需重评（ORSI
  参考依据的失效联动面）。指纹算法此处为基准侧独立计算（sha256，场景集+指标快照），
  与 services/rsi/domain/orsi.py 注册侧指纹**不同源**（注册时由业务层重算，本文件
  只提供建议值供人工比照，不冒充注册面算法）。

用法：
    python benchmarks/orsi_link.py --results benchmarks/results/agent-core/2026-10-07 --out suggestions.json
    python benchmarks/orsi_link.py --results <某个 scenario 结果.json>            # 打印 stdout
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent

# suite → ORSI face 归属（services/rsi/surfaces.py EvolutionSurface 八面枚举）；
# agent-core 六指标均属 O5 控制流程（互斥/越权/重试/恢复=内核控制面），唯一例外
# memory_cross_contamination 属 O6 记忆策略。rag/intent 后续 suite 归 O7/O3 随批登记。
_SUITE_FACE: dict[str, str] = {"agent-core": "O5", "rag": "O7", "intent": "O3", "ontology-scale": "O2"}
# 场景级面覆盖（runner.ORSI_FACE_SUGGESTION 同源；场景级优先于 suite 级）
_SCENARIO_FACE: dict[str, str] = {"memory_cross_contamination": "O6"}


def _face_for(suite: str, scenario: str) -> str:
    return _SCENARIO_FACE.get(scenario, _SUITE_FACE.get(suite, "O5"))


def load_results(results_path: Path) -> list[tuple[Path, dict[str, Any]]]:
    """读结果：单文件或目录（目录=取 *.json，manifest 除外）；路径统一 resolve（evidence_uri 相对化用）。

    citation_only 件（公开榜单引用表，benchmarks/suites/rag/leaderboard_citations/，
    非实测数据）显式跳过——ORSI evidence 只收实测指标，文献引用不进能力注册建议。
    """
    results_path = results_path.resolve()
    if results_path.is_file():
        payload = json.loads(results_path.read_text(encoding="utf-8"))
        if payload.get("nature") == "citation_only":
            return []
        return [(results_path, payload)]
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(results_path.glob("*.json")):
        if path.name.endswith("manifest.json"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("nature") == "citation_only":
            continue
        out.append((path, payload))
    return out


def build_suggestions(pairs: list[tuple[Path, dict[str, Any]]], *, repo_root: Path) -> list[dict[str, Any]]:
    """结果 JSON → 注册建议列表（face/status/evidence_uri/fingerprint/rationale）。"""
    scenario_set = sorted(result.get("scenario", "?") for _, result in pairs)
    scenario_set_hash = hashlib.sha256("\x1f".join(scenario_set).encode("utf-8")).hexdigest()
    suggestions: list[dict[str, Any]] = []
    for path, result in pairs:
        scenario = result.get("scenario", "?")
        suite = result.get("suite", "agent-core")
        metrics = result.get("metrics", {})
        suggestions.append(
            {
                "suggestion_type": "orsi_capability_registration",
                "auto_registered": False,  # 16 篇 §4 本波边界：只建议不注册
                "proposal": {
                    "name": f"bench.{suite}.{scenario}",
                    "face": _face_for(suite, scenario),
                    "status": "candidate",  # 宪法 3：候选非成品；晋升必经人工审核工单
                    "version": "1.0.0",
                },
                "evidence": {
                    "evidence_uri": str(path.relative_to(repo_root)).replace("\\", "/"),
                    "run_status": result.get("status"),
                    "smoke": result.get("smoke"),
                    "benchmark_ref": result.get("benchmark_ref"),
                    "key_metrics": {
                        k: metrics.get(k)
                        for k in sorted(metrics)
                        if isinstance(metrics.get(k), (int, float, bool))
                    },
                },
                "capability_fingerprint": hashlib.sha256(
                    f"bench-scenarios:{scenario_set_hash}:{suite}:{scenario}".encode()
                ).hexdigest(),
                "fingerprint_note": (
                    "基准侧场景集哈希（场景集变=指纹变=能力需重评）；非注册面算法——"
                    "正式注册时由 services/rsi 业务层重算，本值仅供人工比照"
                ),
                "rationale": f"指标实测数据佐证 {scenario} 能力面现状（16 篇 §3 ORSI 参考依据机制）",
            }
        )
    return suggestions


def main() -> int:
    parser = argparse.ArgumentParser(description="ORSI capability 注册建议生成（只建议不注册）")
    parser.add_argument("--results", required=True, help="结果 JSON 文件或目录（benchmarks/results/<suite>/<date>）")
    parser.add_argument("--out", default=None, help="建议 JSON 输出路径（缺省打印 stdout）")
    args = parser.parse_args()
    results_path = Path(args.results)
    if not results_path.exists():
        print(f"[orsi-link] 结果路径不存在: {results_path}", file=sys.stderr)
        return 1
    pairs = load_results(results_path)
    if not pairs:
        print(f"[orsi-link] 无结果 JSON: {results_path}", file=sys.stderr)
        return 1
    suggestions = build_suggestions(pairs, repo_root=_REPO_ROOT)
    payload = {
        "generated_from": str(results_path),
        "scenarios": sorted(result.get("scenario", "?") for _, result in pairs),
        "suggestions": suggestions,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"[orsi-link] 建议已写入: {out_path}（{len(suggestions)} 条；未注册——人工确认后走注册端点）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
