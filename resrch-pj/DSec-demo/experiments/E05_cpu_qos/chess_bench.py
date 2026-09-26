"""LS 基准：固定预算的迭代加深 α-β 搜索（agent 决策步的代理负载，负载形状为重）。"""
import time

INF = 10 ** 9


def ab(depth: int, alpha: int, beta: int, b: int, leaf: dict) -> int:
    if depth == 0:
        leaf["n"] += 1
        return (leaf["n"] * 2654435761) & 0xFFFF - 32768
    if (depth & 1) == 0:  # max 节点
        val = -INF
        for i in range(b):
            val = max(val, ab(depth - 1, alpha, beta, b, leaf))
            alpha = max(alpha, val)
            if alpha >= beta:
                break
        return val
    val = INF
    for i in range(b):
        val = min(val, ab(depth - 1, alpha, beta, b, leaf))
        beta = min(beta, val)
        if beta <= alpha:
            break
    return val


def run_steps(n_steps: int, budget_ms: int = 60) -> list[float]:
    """每步迭代加深至 budget_ms；返回每步耗时 ms。"""
    steps = []
    for _ in range(n_steps):
        t0 = time.perf_counter()
        leaf = {"n": 0}
        d = 4
        while True:
            ab(d, -INF, INF, 6, leaf)
            if (time.perf_counter() - t0) * 1000 > budget_ms or d > 12:
                break
            d += 1
        steps.append((time.perf_counter() - t0) * 1000)
    return steps
