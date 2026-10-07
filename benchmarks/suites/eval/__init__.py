"""eval release 聚合评测套件（docs/Agent/17 §2 批次 B）。

职责边界：**只聚合不重跑**——读 results/ 下四套件已有最近 run JSON，产出
release 级 dashboard.json（核心观测指标+环境指纹+市场引用指针）与 version_diff.json
（本版指标对比）；指标口径唯一事实源=各 suite metrics.py，本套件零指标重算。
"""
