"""agent-core 六指标纯函数计算（口径唯一事实源；docs/Agent/16 §2 第一波冻结）。

纪律：本模块只做确定性数值计算（输入=场景采集的原始观测，输出=结果 JSON 的 metrics
段），禁 IO、禁随机、禁读环境——同输入恒同输出，供 runner 与未来对比曲线复用。
每个函数 docstring 即该指标的口径定义（16 篇 §2「口径冻结进 metrics.py」）。

指标 ↔ 红队审查问题映射（docs/评审/红队攻击性审查-2026-10-06）：
    session_mutex_rate          ← A1 同会话并发消息
    cross_tenant_leak           ← A2 端点矩阵越权
    memory_cross_contamination  ← A3 记忆交叉渗漏
    side_effect_duplication     ← C2 重试副作用重复
    invalid_retry_count         ← C1 恒失败工具无效重试
    recovery_time_s             ← A5/C 族 kill 后恢复时延
"""

from __future__ import annotations

import statistics
from typing import Any

# ---------------------------------------------------------------------------
# ① session_mutex_rate（A1：同 session 并发消息互斥率）
# ---------------------------------------------------------------------------


def session_mutex_rate(accepted_counts: list[int]) -> dict[str, Any]:
    """口径：同 session 并发 N 条消息一轮，被受理（HTTP 2xx）的消息数 ≤1 即「互斥成立」。

    mutex_rate = 互斥成立轮数 / 总轮数（16 篇 §2：bench 攻击脚本并发打同 session，应=1.0）。
    accepted_counts[k] = 第 k 轮并发消息中 2xx 受理数（采集点=send_message 响应状态码）。
    附 rejected_counts / breakdown（拒绝形态分布：4102 预检 / 5xxx 兜底并发冲突等）供归因。
    """
    total = len(accepted_counts)
    held = sum(1 for n in accepted_counts if n <= 1)
    broken = [i for i, n in enumerate(accepted_counts) if n > 1]
    return {
        "iterations": total,
        "mutex_held_iterations": held,
        "mutex_broken_iterations": len(broken),
        "mutex_broken_rounds": [i + 1 for i in broken],  # 轮号（1 起）
        "session_mutex_rate": round(held / total, 6) if total else None,
    }


def mutex_reject_breakdown(reject_codes: list[str]) -> dict[str, int]:
    """口径：并发轮中被拒消息的拒绝形态分布（采集点=非 2xx 响应状态码字符串计数）。"""
    breakdown: dict[str, int] = {}
    for code in reject_codes:
        breakdown[code] = breakdown.get(code, 0) + 1
    return breakdown


# ---------------------------------------------------------------------------
# ② cross_tenant_leak（A2：端点矩阵越权，应全拒）
# ---------------------------------------------------------------------------


def cross_tenant_leak(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """口径：攻击矩阵逐笔判定——HTTP 2xx=越权成功（泄漏），其余（401/403/404/409/4xx/5xx）
    =拒绝。reject_rate = 拒绝笔数 / 总笔数（16 篇 §2「端点矩阵攻击（应 0）」→ 应=1.0）。

    attempts 元素形状：{endpoint, attacker, method, status, body_digest?}（采集点=httpx
    响应状态码；attacker=same_tenant|cross_tenant）。
    """
    total = len(attempts)
    leaked = [a for a in attempts if 200 <= int(a.get("status", 0)) < 300]
    rejected = total - len(leaked)
    return {
        "attacks_total": total,
        "attacks_rejected": rejected,
        "attacks_leaked": len(leaked),
        "reject_rate": round(rejected / total, 6) if total else None,
        "leaked_endpoints": [
            {"endpoint": a.get("endpoint"), "attacker": a.get("attacker"), "status": a.get("status")} for a in leaked
        ],
    }


# ---------------------------------------------------------------------------
# ③ memory_cross_contamination（A3：双用户独特征记忆互查零交叉）
# ---------------------------------------------------------------------------


def memory_cross_contamination(probes: list[dict[str, Any]]) -> dict[str, Any]:
    """口径：双用户各写独特征（内容含唯一标记）记忆后互查，出现他用户标记即渗漏 1 条。

    leak_count = 全部探针中「读到他人独特征标记」的次数（16 篇 §2 应=0）；采集点=
    GET /memory/facts、POST /memory/search 响应体内容。unauthorized_accepted=显式指名
    他人 user_id 的越权读探针中 2xx 笔数（授权矩阵应 403/2002，应=0）。
    probes 元素形状：{probe, reader, marker_owner, seen, unauthorized(bool), status}。
    """
    leak_probes = [p for p in probes if p.get("seen")]
    unauthorized = [p for p in probes if p.get("unauthorized") and 200 <= int(p.get("status", 0)) < 300]
    return {
        "probes_total": len(probes),
        "leak_count": len(leak_probes),
        "leak_probes": [
            {"probe": p.get("probe"), "reader": p.get("reader"), "marker_owner": p.get("marker_owner")}
            for p in leak_probes
        ],
        "unauthorized_accepted": len(unauthorized),
    }


# ---------------------------------------------------------------------------
# ④ side_effect_duplication（C2：重试后 EXTERNAL_WRITE 重复执行数）
# ---------------------------------------------------------------------------


def side_effect_duplication(
    write_counts: list[int], *, key_observations: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """口径（C2 修复批修订 2026-10-07）：含 EXTERNAL_WRITE 步的任务「强制失败→run 级重试」
    全链后，外部写动作执行次数与**幂等键贯通可见性**。

    16 篇 §2 原口径「重复执行数应 1」按红队 §5 修法修订：工具实现侧幂等=后续批，本批
    通过口径=「**重试轮写动作带相同 key**（attempt 维 idempotency_key=task_id:attempt，
    审批工单 param_hash 与工具调用参数同键）」——键可见性验证（键可见+两尝试键齐备）。
    写次数仍留样（extra_writes/duplication_rate 保留为观测面，不作为通过判据）。

    key_observations 元素形状：{task_id, attempt1_key, attempt2_key}（采集点=假工具
    ToolCall.parameters["idempotency_key"]，attempt1/2 各按其命令键过滤命中）。
    """
    runs = len(write_counts)
    total = sum(write_counts)
    duplicated_rounds = [i + 1 for i, n in enumerate(write_counts) if n > 1]
    keys = key_observations or []
    rounds_with_both_keys = sum(1 for k in keys if k.get("attempt1_key") and k.get("attempt2_key"))
    format_ok = bool(keys) and all(
        k.get("attempt1_key") == f"{k.get('task_id')}:1" and k.get("attempt2_key") == f"{k.get('task_id')}:2"
        for k in keys
    )
    return {
        "runs": runs,
        "total_external_writes": total,
        "expected_writes": runs,  # 观测面：应 1/轮（工具侧幂等后续批收敛）
        "extra_writes": total - runs,
        "duplicated_rounds": duplicated_rounds,
        "duplication_rate": round(len(duplicated_rounds) / runs, 6) if runs else None,
        # C2 新通过口径（红队 §5 修复批）：键贯通可见
        "idempotency_key_visible": runs > 0 and rounds_with_both_keys == runs,
        "retry_key_consistent": format_ok,
        "key_rounds_with_both_keys": rounds_with_both_keys,
    }


# ---------------------------------------------------------------------------
# ⑤ invalid_retry_count（C1：恒失败工具的无效重试数）
# ---------------------------------------------------------------------------


def invalid_retry(
    tool_invocations: int, abandoned: bool, abort_threshold: int, plan_steps: int
) -> dict[str, Any]:
    """口径：恒失败工具场景（伪模型注入同签名失败计划）下，同一失败工具被无效重复调用
    的次数 = 工具调用总数 − 1（首次调用不算重试）。两段式循环防护（A-1/K1-a）下应
    有界：调用总数 ≤ abort_threshold+1（首调+第 1 次重复执行、第 abort_threshold 次
    重复前硬终止），且 run 以 EXEC_LOOP_DETECTED(5008) 结构化放弃。

    abandoned=run 终态是否为循环防护终止（采集点=RunOutcome.reason_code==5008）；
    unbounded=True 表示防护未生效（调用数超出阈值界或未放弃）。
    """
    bound = abort_threshold + 1  # 阈值语义：第 1…N−1 次重复执行，第 N 次重复不执行
    unbounded = (tool_invocations > bound) or not abandoned
    return {
        "tool_invocations": tool_invocations,
        "invalid_retry_count": max(0, tool_invocations - 1),
        "abort_threshold": abort_threshold,
        "invocation_bound": bound,
        "abandoned_correctly": abandoned,
        "plan_steps_injected": plan_steps,
        "unbounded_retry": unbounded,
    }


def loop_guard_events(ledger_event_types: list[str]) -> dict[str, int]:
    """口径：内核账本中循环防护相关事件的计数（kernel.loop_nudge 软警告等）——
    采集点=ledger sink 收到的 KernelEvent.event_type 列表。"""
    breakdown: dict[str, int] = {}
    for name in ledger_event_types:
        breakdown[name] = breakdown.get(name, 0) + 1
    return breakdown


# ---------------------------------------------------------------------------
# ⑥ recovery_time_s（A5/C 族：中断后状态收敛+恢复路径时延）
# ---------------------------------------------------------------------------


def recovery_time(
    convergence_samples_s: list[float],
    *,
    converged_flags: list[bool],
    recovery_ok_flags: list[bool],
    zombie_tasks: list[int],
) -> dict[str, Any]:
    """口径：kill 中断（asyncio cancel 打断在途 run）发起时刻 → 任务状态完全收敛
    （取消清单执行完、内核任务退出、无僵尸任务）的墙钟时延（秒）。恢复路径断言=
    收敛后同会话新一轮对话可正常完成（recovery_ok）。16 篇 §2 采集点=进程内计时器
    （time.monotonic），进程屠杀（kill -9）形态登记后续批次。

    报告 n/p50/p95/max（秒）+ converged_rate / recovery_rate / zombie_tasks_total。
    """
    n = len(convergence_samples_s)
    ordered = sorted(convergence_samples_s)

    def _pct(p: float) -> float | None:
        if not ordered:
            return None
        idx = min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))
        return round(ordered[idx], 4)

    return {
        "iterations": n,
        "recovery_time_s_p50": _pct(0.50),
        "recovery_time_s_p95": _pct(0.95),
        "recovery_time_s_max": round(max(ordered), 4) if ordered else None,
        "recovery_time_s_mean": round(statistics.fmean(ordered), 4) if ordered else None,
        "converged_rate": round(sum(1 for f in converged_flags if f) / n, 6) if n else None,
        "recovery_rate": round(sum(1 for f in recovery_ok_flags if f) / n, 6) if n else None,
        "zombie_tasks_total": sum(zombie_tasks),
    }
