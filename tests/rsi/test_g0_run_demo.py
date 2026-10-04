# tests/rsi/test_g0_run_demo.py
"""run_g0 demo 模式端到端用例（subprocess 真跑 services/devtools/orsi/run_g0.py --demo）。

断言目标：退出码 0；报告含簇明细（主簇 opened / 对照簇 below_threshold）与 proposal id；
工单留痕 JSONL 落盘且 surface=O1；demo 标注在场（合成数据非真实信号）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_run_g0_demo端到端(tmp_path: Path) -> None:
    store_dir = tmp_path / "store"
    report_path = tmp_path / "report.md"
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "services" / "tools" / "orsi" / "run_g0.py"),
            "--demo",
            "--store-dir",
            str(store_dir),
            "--report",
            str(report_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "[demo] 注入合成缺口事件 8 条" in proc.stdout  # ⚠ 标注：非真实信号
    assert "主簇 6 条同指纹 + 对照簇 2 条" in proc.stdout

    report = report_path.read_text(encoding="utf-8")
    assert "# ORSI G0 缺口轨运行报告" in report
    assert "demo（⚠ 合成数据，非真实信号）" in report
    assert "| opened |" in report and "| below_threshold |" in report  # 主簇开单 / 对照簇阈值边界
    assert "① **组合既有工具**" in report  # G1 三级降路径提示
    assert "surface=O1" in report

    # 报告中的 proposal id 与工单留痕一致（JSONL 单行，surface=O1）
    record_lines = (store_dir / "gap_proposals.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in record_lines if line]
    assert len(records) == 1
    assert records[0]["surface"] == "O1" and records[0]["kind"] == "execution_failure"
    assert records[0]["proposal_id"] in report
    events = [
        json.loads(line) for line in (store_dir / "gap_events.jsonl").read_text(encoding="utf-8").splitlines() if line
    ]
    assert len(events) == 8  # 6 主簇 + 2 对照，全量留痕可复现


def test_run_g0_参数校验_双模式互斥_live须租户() -> None:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    base = [sys.executable, str(REPO_ROOT / "services" / "tools" / "orsi" / "run_g0.py")]
    for args in (
        ["--demo", "--live"],  # 互斥
        [],  # 必选其一
        ["--live"],  # live 缺租户
        ["--live", "--tenant-id", "not-a-uuid"],  # 租户非法 UUID
    ):
        proc = subprocess.run(base + args, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", env=env)
        assert proc.returncode != 0, f"args={args} 应拒绝"
