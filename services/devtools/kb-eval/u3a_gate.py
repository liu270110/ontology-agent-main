#!/usr/bin/env python3
"""U-③a 批次门禁唯一入口（标准库 subprocess 转发 pytest 退出码；本文件即文档化产物）。

用法（仓库根目录；脚本自定位 worktree 根为 cwd）：
    python services/devtools/kb-eval/u3a_gate.py            # 全量套件（= python -m pytest -q）
    python services/devtools/kb-eval/u3a_gate.py --u3a      # 仅 -m vllm 真模型场景（127.0.0.1:18001 /v1；
                                                         #   无服务自动 skip，ollama marker 同款纪律）

输出纪律（2026-10-05 增补：e2e 期间管线日志达数百 KB，超调用方 stdout 捕获上限 256KB 被拒）：
- 完整输出落盘 ``services/devtools/kb-eval/.u3a_gate_last.log``（UTF-8，路径随首行打印，每次运行
  覆盖；边跑边写，中途 kill 亦留已产出部分）；
- stdout 只回显 pytest 输出头部 40 行 + 尾部 120 行 + 截断提示（'…中间 N 行见日志文件…'）；
- 退出码语义一字不动：不解释不吞——pytest 退出码即门禁退出码。

门禁纪律（2026-10-05 批次规格）：全量套件只由本门禁跑，实现者不自行跑全量；
本脚本零第三方依赖（纯 stdlib）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LOG_PATH = Path(__file__).resolve().parent / ".u3a_gate_last.log"
_HEAD_LINES = 40  # stdout 回显头部行数（完整输出恒在 LOG_PATH）
_TAIL_LINES = 120  # stdout 回显尾部行数
_PREAMBLE_LINES = 2  # 门禁自写日志头（cwd + 命令），裁剪时剔除、只裁 pytest 输出


def _trim(lines: list[str], *, head: int = _HEAD_LINES, tail: int = _TAIL_LINES) -> tuple[list[str], int]:
    """stdout 回显裁剪（纯函数）：头部 head 行 + 尾部 tail 行 + 截断提示。

    返回 (回显行, 省略的中间行数)；总行数 ≤ head+tail 时原样全回、省略数 0（不打提示）。
    """
    omitted = len(lines) - head - tail
    if omitted <= 0:
        return lines, 0
    return [*lines[:head], f"…中间 {omitted} 行见日志文件…", *lines[-tail:]], omitted


def main() -> int:
    parser = argparse.ArgumentParser(description="U-③a 批次门禁（全量 / --u3a 真模型场景）")
    parser.add_argument("--u3a", action="store_true", help="仅跑 -m vllm 真模型场景（缺省=全量套件）")
    args = parser.parse_args()

    cmd = [sys.executable, "-m", "pytest", "-q"]
    if args.u3a:
        cmd += ["-m", "vllm"]
    print(f"[u3a_gate] cwd={REPO_ROOT} | 完整日志: {LOG_PATH}")
    print(f"[u3a_gate] $ {' '.join(cmd)}", flush=True)

    returncode: int
    with LOG_PATH.open("w", encoding="utf-8") as log:
        log.write(f"[u3a_gate] cwd={REPO_ROOT}\n[u3a_gate] $ {' '.join(cmd)}\n")
        log.flush()
        # stderr 并入同一文件（pytest 段落日志走 stderr；退出码原样返回，不解释不吞）
        returncode = subprocess.call(cmd, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT)
    pytest_lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[_PREAMBLE_LINES:]
    echo, _omitted = _trim(pytest_lines)
    print("\n".join(echo), flush=True)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
