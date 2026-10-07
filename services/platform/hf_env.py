"""HF 下载镜像环境缺省（platform 层；07 契约 §6 环境面唯一出口——能力层禁直读 env，D1）。

先例：services/devtools/jev-local/demo_jev.py:21（国内网络走 hf-mirror；respect 调用方
已配置值——setdefault 语义）。消费方=懒加载可选模型引擎（agent capabilities/jev/engine.py
等）；kb 的 docling 引擎下载指引同源（kb/business/parsers.py:6 注释）。

为什么在 platform 层做：tests/agent/test_boundary_discipline.py::test_能力层禁直读环境变量
（07 契约 D1）强制 capabilities 目录 os.environ 零命中——环境写入与读取一样属配置面，
统一收口本模块（可变值经 Settings 注入的纪律在引擎构造参数侧另行保持）。
"""

from __future__ import annotations

import os


def apply_hf_mirror_defaults() -> None:
    """设置 HF 镜像缺省环境（幂等；已显式配置的值不被覆盖）。"""
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # 镜像下 xet 传输不稳（demo 先例注释）
