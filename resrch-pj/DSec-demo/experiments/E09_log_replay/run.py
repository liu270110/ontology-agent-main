#!/usr/bin/env python3
"""E9 抢占恢复：命令日志重放（论文 §6.2 V4.1 之前的 agent loop 恢复机制）。

GPU 作业被抢占 → agent loop 丢失而沙箱仍在 → 恢复时对齐已完成的操作：
已完成命令复用记录结果而非重执行（非幂等命令不产生重复副作用）。
"""
import json
import os

out = {"experiment": "E9_log_replay"}


class Session:
    """最小 agent 会话：命令日志 + 沙箱状态游标。"""

    def __init__(self):
        self.log: list[dict] = []   # {"cmd", "exit", "stdout", "done"}
        self.counter = 0            # 沙箱内非幂等副作用计数器（模拟 echo >> file）

    def _exec(self, cmd: str) -> dict:
        # 模拟真实执行：counter++ 是非幂等副作用
        self.counter += 1
        return {"cmd": cmd, "exit": 0, "stdout": f"ok({self.counter})", "done": True}

    def step(self, cmd: str) -> dict:
        return self._exec(cmd)

    def preempt(self) -> list[dict]:
        """GPU 被抢占：已持久化的命令日志留存。"""
        return self.log

    def resume(self, plan: list[str]) -> dict:
        """恢复：逐条对齐——日志中已完成的跳过（复用结果），只执行剩余。"""
        executed = 0
        reused = 0
        for i, cmd in enumerate(plan):
            if i < len(self.log) and self.log[i].get("done"):
                reused += 1
                continue
            self.log.append(self._exec(cmd))
            executed += 1
        return {"executed": executed, "reused": reused}


s = Session()
plan = ["git clone repo", "pip install -r req.txt", "run tests", "collect reward"]

# 阶段1：跑了两条后被抢占
s.log = []
s.log.append(s._exec(plan[0]))
s.log.append(s._exec(plan[1]))
preempt_at = s.counter
resumed = s.resume(plan)
final_counter = s.counter

out["preempt_counter"] = preempt_at          # = 2（clone、install 各执行一次）
out["resume_executed"] = resumed["executed"]  # 只补剩余 2 条
out["resume_reused"] = resumed["reused"]      # 复用 2 条
out["final_counter"] = final_counter          # = 4 而非 6（非幂等命令没有重复执行）
out["no_duplicate_side_effects"] = final_counter == 4 and resumed["reused"] == 2 and resumed["executed"] == 2
out["full_log_len"] = len(s.log)

# 对照：无日志重放的朴素重跑 → 副作用翻倍
naive = Session()
naive.log = []
for cmd in plan[:2]:
    naive.log.append(naive._exec(cmd))
for cmd in plan:  # 抢占后从头重跑
    naive._exec(cmd)
out["naive_rerun_counter"] = naive.counter  # = 6
out["naive_duplicate_side_effects"] = naive.counter == 6

out["passed"] = out["no_duplicate_side_effects"] and out["naive_duplicate_side_effects"]
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "results", "E9_log_replay.json"), "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(json.dumps(out, indent=2))
