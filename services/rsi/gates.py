"""RSI 三级评估门禁（architecture/09 §4 的阶段 A 演练位骨架）。

门禁链（09 §4）：0 级硬门槛（清单校验 + 信封静态检查，毫秒级，不过即拒）→ ①沙箱回放 →
②金标回归 → ③灰度对比。三档治理底线：三级门禁是硬门禁，任何档位不可跳过（08 §2.4）。

阶段 A 边界（收缩裁决，见模块报告）：本文件只交付**门禁链框架**——0 级机械校验真跑；
①②③为「not_configured（M5+ 启用）」演练位：评估运行是资源型任务（沙箱供给=模块 13、
评估基准=08 §7 评估体系、灰度观测=晋级管线），随阶段 B 详设接入；演练位结论不作为任何
生效依据（apply 恒拒与之互锁）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.rsi.proposal import ENVELOPE_REQUIRED_KEYS, Proposal
from services.rsi.whitelist import validate_improvement

VERDICT_PASS = "pass"
VERDICT_NOT_CONFIGURED = "not_configured:M5+"
VERDICT_PREFIX_FAIL = "fail"


@dataclass(frozen=True, slots=True)
class GateResult:
    """单级门禁结论（09 §7 eval_report 的行粒度）。"""

    gate: str
    passed: bool
    verdict: str  # pass | fail:<原因> | not_configured:M5+


def _fail(gate: str, reason: str) -> GateResult:
    return GateResult(gate=gate, passed=False, verdict=f"{VERDICT_PREFIX_FAIL}:{reason}")


def run_level0(proposal: Proposal) -> GateResult:
    """0 级硬门槛（09 §4 前置行）：清单校验（五类白名单 + 目标禁区）+ 信封静态检查。"""
    gate = "level0_whitelist_schema"
    try:
        validate_improvement(proposal.type.value, proposal.target)
    except Exception as exc:  # noqa: BLE001 ——WhitelistViolation 即拒（安全审计由服务层落）
        return _fail(gate, str(exc))
    missing = [key for key in ENVELOPE_REQUIRED_KEYS if key not in proposal.envelope]
    if missing:
        return _fail(gate, f"统一信封缺键: {','.join(missing)}（09 §2 信封契约）")
    if not proposal.source_trace_ids:
        return _fail(gate, "缺少来源轨迹（证据链逐环可回链，09 §1 宪法 3）")
    return GateResult(gate=gate, passed=True, verdict=VERDICT_PASS)


def run_level1(proposal: Proposal) -> GateResult:
    """①沙箱回放演练位：抽样历史任务轨迹在评测沙箱以候选改进重放（M5+，依赖模块 13 供给）。"""
    _ = proposal
    return GateResult(gate="level1_sandbox_replay", passed=False, verdict=VERDICT_NOT_CONFIGURED)


def run_level2(proposal: Proposal) -> GateResult:
    """②金标回归演练位：08 §7 三类评估基准全量跑分对比基线（M5+，评测集冻结治理前置）。"""
    _ = proposal
    return GateResult(gate="level2_golden_regression", passed=False, verdict=VERDICT_NOT_CONFIGURED)


def run_level3(proposal: Proposal) -> GateResult:
    """③灰度对比演练位：高风险类灰度期观测（M5+；观测期 7 天或 ≥100 任务）。"""
    _ = proposal
    return GateResult(gate="level3_gray_compare", passed=False, verdict=VERDICT_NOT_CONFIGURED)


def evaluate_chain(proposal: Proposal) -> tuple[bool, list[GateResult]]:
    """门禁链：0 级 fail 即短路（其余级不跑）；0 级过 → ①②③演练位结论集（不做生效判定）。"""
    level0 = run_level0(proposal)
    if not level0.passed:
        return False, [level0]
    return True, [level0, run_level1(proposal), run_level2(proposal), run_level3(proposal)]


def build_eval_report(results: list[GateResult], *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """eval_report 组装（09 §7：三级门禁结论 + 基线 delta 位；阶段 A 基线 delta 留空）。"""
    report: dict[str, Any] = {
        "gates": [{"gate": r.gate, "passed": r.passed, "verdict": r.verdict} for r in results],
        "baseline_delta": None,  # 基线对比随 ② 金标回归接入（M5+）
        "stage": "A_skeleton",
    }
    if extra:
        report.update(extra)
    return report
