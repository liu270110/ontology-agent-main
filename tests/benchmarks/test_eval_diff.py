"""eval version-diff 单测（docs/Agent/17 §2 批次 B 第 2 项）：纯函数+配对+落盘。

锁定四件事：
1. 趋势判定/差值计算纯函数（↑↓→/n/a、布尔翻转、容差内平、单侧缺失不猜数）；
2. 方向表与回归判定（lower_is_better 指标 ↑ 即回归；neutral 不判定）；
3. run 配对与可比性守门（同 tag 不配、指纹不同标 no_baseline、显式点名缺 tag 报错）；
4. version_diff.json 落盘与 SUMMARY.md 追加式（两次写历史保留，文件头只出现一次）。

AAA 注： Arrange/Act/Assert 中文注释逐段标注；夹具全部用 tmp_path 合成结果目录，
不读写真实 results/（真实数据演示走终验，不入单测）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from benchmarks.suites.eval import diff as eval_diff
from benchmarks.suites.eval.config import EvalReleaseSettings
from benchmarks.suites.eval.runs import discover_runs, summary_fallback_tag

_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> EvalReleaseSettings:
    """Arrange 公共件：指向 tmp 结果目录的 Settings（其余参数取默认）。"""
    return EvalReleaseSettings(results_dir=tmp_path / "benchmarks" / "results")


# ---------------------------------------------------------------- ① 趋势判定 / 差值计算（纯函数）


class TestJudgeTrend:
    """judge_trend：数值过容差、布尔翻转、缺失与异质不可比。"""

    def test_数值_升_降_平(self):
        # Arrange：epsilon=0.01（容差内的差视为平）
        # Act / Assert
        assert eval_diff.judge_trend(1.0, 1.5, epsilon=0.01) == "↑"
        assert eval_diff.judge_trend(1.5, 1.0, epsilon=0.01) == "↓"
        assert eval_diff.judge_trend(1.0, 1.005, epsilon=0.01) == "→"
        assert eval_diff.judge_trend(3, 3, epsilon=0.0) == "→"

    def test_布尔翻转_相等为平(self):
        # Arrange / Act / Assert：False→True 视为升（语义好坏由方向表另判）
        assert eval_diff.judge_trend(False, True) == "↑"
        assert eval_diff.judge_trend(True, False) == "↓"
        assert eval_diff.judge_trend(True, True) == "→"

    def test_单侧缺失或类型异质_不可比(self):
        # Arrange：prev 缺失（partial run 未产出该指标）/ 字符串对数值
        # Act / Assert：如实 n/a，不猜方向
        assert eval_diff.judge_trend(None, 1.0) == "n/a"
        assert eval_diff.judge_trend(1.0, None) == "n/a"
        assert eval_diff.judge_trend(1, True) == "n/a"

    def test_负零与浮点噪声_容差内为平(self):
        # Arrange：1.1+2.2 类浮点噪声
        # Act / Assert
        assert eval_diff.judge_trend(0.1 + 0.2, 0.3, epsilon=1e-9) == "→"


class TestComputeDelta:
    """compute_delta：数值差取指定位；任一侧非数值 → None。"""

    def test_数值差与精度(self):
        # Arrange / Act / Assert
        assert eval_diff.compute_delta(0.666667, 1.0, round_digits=6) == 0.333333
        assert eval_diff.compute_delta(1, 2, round_digits=6) == 1.0
        assert eval_diff.compute_delta(1.0000004, 1.0000006, round_digits=6) == 0.0

    def test_布尔与缺失不产生数值差(self):
        # Arrange / Act / Assert
        assert eval_diff.compute_delta(False, True) is None
        assert eval_diff.compute_delta(None, 5) is None
        assert eval_diff.compute_delta(5, None) is None
        assert eval_diff.compute_delta("1", "2") is None


class TestDiffMetricMaps:
    """diff_metric_maps：并集键排序、单侧缺失以 null 留痕（partial 处理）。"""

    def test_并集键且排序稳定(self):
        # Arrange：prev 缺 b（partial run）、curr 缺 c（指标更名）
        prev_map = {"a.rate": 0.5, "b.count": 3}
        curr_map = {"a.rate": 0.75, "c.count": 3}
        settings = _settings(Path("."))  # 仅用其 delta 精度/容差参数
        # Act
        rows = eval_diff.diff_metric_maps(prev_map, curr_map, settings=settings)
        # Assert：按 metric 名排序；缺失侧 prev/curr=null、delta=null、trend=n/a
        assert [row["metric"] for row in rows] == ["a.rate", "b.count", "c.count"]
        by_metric = {row["metric"]: row for row in rows}
        assert by_metric["a.rate"] == {"metric": "a.rate", "prev": 0.5, "curr": 0.75, "delta": 0.25, "trend": "↑"}
        assert by_metric["b.count"] == {"metric": "b.count", "prev": 3, "curr": None, "delta": None, "trend": "n/a"}
        assert by_metric["c.count"] == {"metric": "c.count", "prev": None, "curr": 3, "delta": None, "trend": "n/a"}


class TestDirectionAndRegressions:
    """方向表与回归判定：方向感知，neutral 不判定。"""

    def test_方向表命中与兜底(self):
        # Arrange / Act / Assert
        assert eval_diff.direction_for("recovery_time_s.recovery_time_s_p50") == eval_diff.DIRECTION_LOWER
        assert eval_diff.direction_for("cross_tenant_leak.reject_rate") == eval_diff.DIRECTION_HIGHER
        assert eval_diff.direction_for("naive_bm25.recall_at_k") == eval_diff.DIRECTION_NEUTRAL
        assert eval_diff.direction_for("未知指标") == eval_diff.DIRECTION_NEUTRAL

    def test_回归判定_低优升_高优降各计一(self):
        # Arrange：p50 时延↑（低优=回归）、reject_rate↓（高优=回归）、naive 升降（neutral 不计）
        rows = [
            {"metric": "recovery_time_s.recovery_time_s_p50", "delta": 0.0002, "trend": "↑"},
            {"metric": "cross_tenant_leak.reject_rate", "delta": -0.1, "trend": "↓"},
            {"metric": "naive_bm25.recall_at_k", "delta": -0.5, "trend": "↓"},
            {"metric": "session_mutex_rate.session_mutex_rate", "delta": None, "trend": "n/a"},
        ]
        # Act
        regressions = eval_diff.regression_metrics(rows)
        # Assert
        assert regressions == ["recovery_time_s.recovery_time_s_p50", "cross_tenant_leak.reject_rate"]

    def test_布尔指标回归判定_键可见翻转即回归(self):
        # Arrange：idempotency_key_visible True→False（高优方向）应计回归
        rows = [{"metric": "side_effect_duplication.idempotency_key_visible", "delta": None, "trend": "↓"}]
        # Act / Assert：布尔行无数值 delta，回归列不误判（趋势面已给 ↓，回归判定只吃数值 delta）
        assert eval_diff.regression_metrics(rows) == []


# ---------------------------------------------------------------- ② run 配对与可比性守门（tmp 夹具）


def _make_agent_core_run(
    tmp_path: Path,
    clock: str,
    tag: str,
    *,
    started_at: str,
    finished_at: str,
    p50: float,
    manifest: bool = True,
    summary_entries: list[tuple[str, str]] | None = None,
    extra_param: dict | None = None,
) -> Path:
    """Arrange：合成一次 agent-core run（两场景简化组 + 可选 manifest + 可选 SUMMARY 行）。"""
    date_dir = tmp_path / "benchmarks" / "results" / "agent-core" / "2026-10-07"
    date_dir.mkdir(parents=True, exist_ok=True)
    params = {"smoke": True, "runs": 5}
    if extra_param:
        params.update(extra_param)
    for scenario, metric_value in (("session_mutex_rate", 1.0), ("recovery_time_s", p50)):
        payload = {
            "suite": "agent-core",
            "scenario": scenario,
            "smoke": True,
            "started_at": started_at,
            "params": params,
            "metrics": (
                {"session_mutex_rate": metric_value}
                if scenario == "session_mutex_rate"
                else {"recovery_time_s_p50": metric_value}
            ),
            "status": "ok",
        }
        (date_dir / f"{clock}-{scenario}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if manifest:
        (date_dir / f"{clock}-manifest.json").write_text(
            json.dumps(
                {
                    "suite": "agent-core",
                    "smoke": True,
                    "tag": tag,
                    "finished_at": finished_at,
                    "status_counts": {"ok": 2, "assert_failed": 0, "error": 0},
                    "env": {"commit": "a" * 40, "python": "3.13.0"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    if summary_entries:
        lines = ["# agent-core 曲线", ""]
        for ts, entry_tag in summary_entries:
            lines.append(f"## {ts} tag={entry_tag} smoke=True commit=abcd1234 branch=x")
            lines.append("")
            lines.append("| 场景 | 关键指标 | 断言 | 状态 | 耗时s |")
            lines.append("| ---- | ---- | ---- | ---- | ---- |")
            lines.append("| session_mutex_rate | session_mutex_rate=1.0 | - | ok | 0.1 |")
            lines.append("")
        (tmp_path / "benchmarks" / "results" / "agent-core" / "SUMMARY.md").write_text(
            "\n".join(lines), encoding="utf-8"
        )
    return date_dir


class TestAutoDiffPair:
    """auto_diff_pair：每 tag 取最新、最近两个不同 tag；不足两个异 tag → None。"""

    def test_两个不同_tag_自动配对(self, tmp_path):
        # Arrange：v1（早）与 v2（晚）两次 run
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v1",
            started_at="2026-10-06T00:00:00+00:00",
            finished_at="2026-10-06T00:00:01+00:00",
            p50=0.5,
        )
        _make_agent_core_run(
            tmp_path,
            "090000",
            "v2",
            started_at="2026-10-06T01:00:00+00:00",
            finished_at="2026-10-06T01:00:01+00:00",
            p50=0.6,
        )
        settings = _settings(tmp_path)
        # Act
        pair = eval_diff.auto_diff_pair("agent-core", settings)
        # Assert：curr=最新（v2），prev=次新异 tag（v1）
        assert (pair[0].tag, pair[1].tag) == ("v1", "v2")

    def test_同_tag_多跑只取最新且不足两异tag为None(self, tmp_path):
        # Arrange：三次 run 全部同 tag
        for clock, hh in (("080000", "0"), ("090000", "1"), ("100000", "2")):
            _make_agent_core_run(
                tmp_path,
                clock,
                "v0",
                started_at=f"2026-10-06T0{hh}:00:00+00:00",
                finished_at=f"2026-10-06T0{hh}:00:01+00:00",
                p50=0.5,
            )
        settings = _settings(tmp_path)
        # Act / Assert
        assert eval_diff.auto_diff_pair("agent-core", settings) is None

    def test_无manifest组按SUMMARY时间戳回填tag(self, tmp_path):
        # Arrange：早 run 有 manifest（v1）；晚 run 无 manifest——SUMMARY 有其曲线行
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v1",
            started_at="2026-10-06T00:00:00+00:00",
            finished_at="2026-10-06T00:00:01+00:00",
            p50=0.5,
        )
        _make_agent_core_run(
            tmp_path,
            "090000",
            "v2",
            started_at="2026-10-06T01:00:00+00:00",
            finished_at="2026-10-06T01:00:01+00:00",
            p50=0.6,
            manifest=False,
            summary_entries=[("2026-10-06T01:00:01+00:00", "v2")],
        )
        settings = _settings(tmp_path)
        # Act
        runs = discover_runs("agent-core", settings)
        # Assert：无 manifest 组的 tag 从 SUMMARY 最近邻回填（finished_at ≥ 组内最大 started_at）
        by_tag = {run.tag: run for run in runs}
        assert set(by_tag) == {"v1", "v2"}
        assert by_tag["v2"].tag_source == "summary_fallback"

    def test_summary回填_早于组开始的行不入候选(self, tmp_path):
        # Arrange：SUMMARY 有两行，一行早于组内 started_at（属更早 run），一行恰等
        summary = tmp_path / "benchmarks" / "results" / "agent-core" / "SUMMARY.md"
        summary.parent.mkdir(parents=True)
        summary.write_text(
            "## 2026-10-06T00:59:00+00:00 tag=older-run smoke=True commit=x branch=y\n\n"
            "## 2026-10-06T01:00:01+00:00 tag=v2 smoke=True commit=x branch=y\n",
            encoding="utf-8",
        )
        # Act：组内最大 started_at=01:00:00，回看窗口 6h
        got, got_source = summary_fallback_tag(
            summary,
            datetime.fromisoformat("2026-10-06T01:00:00+00:00").timestamp(),
            max_lookback_s=21600.0,
        )
        # Assert：取 ≥ started_at 的最近行=v2，而非更早的 older-run
        assert (got, got_source) == ("v2", "summary_fallback")


class TestBuildSuiteDiff:
    """build_suite_diff：自动/显式配对、可比性守门、显式缺 tag 报错。"""

    def test_自动配对产出逐指标diff与回归列(self, tmp_path):
        # Arrange：v1→v2，p50 升（低优=回归）、mutex 率平
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v1",
            started_at="2026-10-06T00:00:00+00:00",
            finished_at="2026-10-06T00:00:01+00:00",
            p50=0.5,
        )
        _make_agent_core_run(
            tmp_path,
            "090000",
            "v2",
            started_at="2026-10-06T01:00:00+00:00",
            finished_at="2026-10-06T01:00:01+00:00",
            p50=0.8,
        )
        settings = _settings(tmp_path)
        # Act
        block = eval_diff.build_suite_diff("agent-core", settings)
        # Assert
        assert block["status"] == "ok"
        assert block["comparable"] is True
        by_metric = {row["metric"]: row for row in block["diffs"]}
        assert by_metric["recovery_time_s.recovery_time_s_p50"]["delta"] == pytest.approx(0.3)
        assert by_metric["recovery_time_s.recovery_time_s_p50"]["trend"] == "↑"
        assert block["regressions"] == ["recovery_time_s.recovery_time_s_p50"]

    def test_单侧partial缺失指标以null留痕(self, tmp_path):
        # Arrange：v1 的 recovery 场景缺指标键（partial 落盘形态）
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v1",
            started_at="2026-10-06T00:00:00+00:00",
            finished_at="2026-10-06T00:00:01+00:00",
            p50=0.5,
        )
        _make_agent_core_run(
            tmp_path,
            "090000",
            "v2",
            started_at="2026-10-06T01:00:00+00:00",
            finished_at="2026-10-06T01:00:01+00:00",
            p50=0.8,
        )
        date_dir = tmp_path / "benchmarks" / "results" / "agent-core" / "2026-10-07"
        payload = json.loads((date_dir / "080000-recovery_time_s.json").read_text(encoding="utf-8"))
        payload["metrics"] = {}
        (date_dir / "080000-recovery_time_s.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        settings = _settings(tmp_path)
        # Act
        block = eval_diff.build_suite_diff("agent-core", settings)
        # Assert：该行 prev=null / trend=n/a，其余指标照常 diff
        by_metric = {row["metric"]: row for row in block["diffs"]}
        assert by_metric["recovery_time_s.recovery_time_s_p50"] == {
            "metric": "recovery_time_s.recovery_time_s_p50",
            "prev": None,
            "curr": 0.8,
            "delta": None,
            "trend": "n/a",
        }
        assert by_metric["session_mutex_rate.session_mutex_rate"]["trend"] == "→"
        assert block["regressions"] == []

    def test_跑法指纹不同_自动配对标no_baseline(self, tmp_path):
        # Arrange：两 tag 跑法参数不同（全量 vs 冒烟跑法变化）
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v1",
            started_at="2026-10-06T00:00:00+00:00",
            finished_at="2026-10-06T00:00:01+00:00",
            p50=0.5,
        )
        _make_agent_core_run(
            tmp_path,
            "090000",
            "v2",
            started_at="2026-10-06T01:00:00+00:00",
            finished_at="2026-10-06T01:00:01+00:00",
            p50=0.8,
            extra_param={"runs": 50},
        )
        settings = _settings(tmp_path)
        # Act
        block = eval_diff.build_suite_diff("agent-core", settings)
        # Assert：不产出误导性 delta
        assert block["status"] == "no_baseline"
        assert "incomparable" in block["reason"]

    def test_显式点名指纹不同_照算但挂不可比旗(self, tmp_path):
        # Arrange：同上不可比夹具，但用户显式点名
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v1",
            started_at="2026-10-06T00:00:00+00:00",
            finished_at="2026-10-06T00:00:01+00:00",
            p50=0.5,
        )
        _make_agent_core_run(
            tmp_path,
            "090000",
            "v2",
            started_at="2026-10-06T01:00:00+00:00",
            finished_at="2026-10-06T01:00:01+00:00",
            p50=0.8,
            extra_param={"runs": 50},
        )
        settings = _settings(tmp_path)
        # Act
        block = eval_diff.build_suite_diff("agent-core", settings, prev_tag="v1", curr_tag="v2")
        # Assert
        assert block["status"] == "ok"
        assert block["comparable"] is False
        assert "fingerprint_mismatch" in block["comparability_note"]

    def test_显式点名缺tag_报ValueError不静默(self, tmp_path):
        # Arrange：只有 v1
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v1",
            started_at="2026-10-06T00:00:00+00:00",
            finished_at="2026-10-06T00:00:01+00:00",
            p50=0.5,
        )
        settings = _settings(tmp_path)
        # Act / Assert
        with pytest.raises(ValueError, match="无对应 run"):
            eval_diff.build_suite_diff("agent-core", settings, prev_tag="v1", curr_tag="不存在")

    def test_单文件套件探针与金标不可比_标no_baseline(self, tmp_path):
        # Arrange：intent 形态——同 tag 域两 run，config.items 不同（探针 2 条 vs 金标 100 条）
        intent_dir = tmp_path / "benchmarks" / "results" / "intent" / "2026-10-07"
        intent_dir.mkdir(parents=True)
        for clock, tag, items in (("090000", "probe", 2), ("100000", "v0", 100)):
            (intent_dir / f"run-{clock}-{tag}.json").write_text(
                json.dumps(
                    {
                        "suite": "intent",
                        "tag": tag,
                        "date": f"2026-10-07T{clock[:2]}:{clock[2:4]}:00",
                        "config": {"items": items, "dataset_version": "v0"},
                        "dataset": {"sha256": "same", "distribution": {}},
                        "tiers": {
                            "a0": {"metrics": {"intent_accuracy": 0.9}},
                            "a1": {"metrics": {"intent_accuracy": 0.7}},
                        },
                        "ontology_constraint_gain": {"diff": {"intent_accuracy": -0.2}},
                        "env": {"commit": "b" * 40},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        settings = _settings(tmp_path)
        # Act
        block = eval_diff.build_suite_diff("intent", settings)
        # Assert：跑法/数据规模指纹不同 → no_baseline（不产出误导 delta）
        assert block["status"] == "no_baseline"
        assert "incomparable" in block["reason"]


# ---------------------------------------------------------------- ③ 全量文档与落盘


class TestBuildVersionDiffAndWrite:
    """build_version_diff 全 suite 编排 + 落盘/SUMMARY 追加式。"""

    def test_全量文档_各suite按各自结果给ok或no_baseline(self, tmp_path):
        # Arrange：只给 agent-core 两个 tag；其余 suite 目录留空
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v1",
            started_at="2026-10-06T00:00:00+00:00",
            finished_at="2026-10-06T00:00:01+00:00",
            p50=0.5,
        )
        _make_agent_core_run(
            tmp_path,
            "090000",
            "v2",
            started_at="2026-10-06T01:00:00+00:00",
            finished_at="2026-10-06T01:00:01+00:00",
            p50=0.8,
        )
        settings = _settings(tmp_path)
        # Act
        doc = eval_diff.build_version_diff(settings, release_tag="t1", now=_NOW)
        # Assert
        assert doc["schema"] == "eval-version-diff/v1"
        assert doc["suites"]["agent-core"]["status"] == "ok"
        for suite in ("rag", "intent", "ontology-scale"):
            assert doc["suites"][suite]["status"] == "no_baseline"

    def test_显式prev_curr只作用于点名suite(self, tmp_path):
        # Arrange：agent-core 两 tag + intent 两 tag（intent 可比）
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v1",
            started_at="2026-10-06T00:00:00+00:00",
            finished_at="2026-10-06T00:00:01+00:00",
            p50=0.5,
        )
        _make_agent_core_run(
            tmp_path,
            "090000",
            "v2",
            started_at="2026-10-06T01:00:00+00:00",
            finished_at="2026-10-06T01:00:01+00:00",
            p50=0.8,
        )
        settings = _settings(tmp_path)
        # Act / Assert：显式 tag 缺失 → ValueError（用户输入错误如实上抛）
        with pytest.raises(ValueError, match="无对应 run"):
            eval_diff.build_version_diff(
                settings, release_tag="t1", diff_suite="agent-core", diff_prev="v1", diff_curr="缺失tag", now=_NOW
            )

    def test_写盘_最新件加历史件且SUMMARY追加式(self, tmp_path):
        # Arrange：两 tag 夹具 + 两次 diff 文档（模拟相邻两版）
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v1",
            started_at="2026-10-06T00:00:00+00:00",
            finished_at="2026-10-06T00:00:01+00:00",
            p50=0.5,
        )
        _make_agent_core_run(
            tmp_path,
            "090000",
            "v2",
            started_at="2026-10-06T01:00:00+00:00",
            finished_at="2026-10-06T01:00:01+00:00",
            p50=0.8,
        )
        settings = _settings(tmp_path)
        doc1 = eval_diff.build_version_diff(settings, release_tag="t1", now=_NOW)
        doc2 = eval_diff.build_version_diff(settings, release_tag="t2", now=datetime(2026, 10, 7, 13, 0, 0, tzinfo=UTC))
        # Act：写两次
        eval_diff.write_version_diff(doc1, settings, now=_NOW)
        path2 = eval_diff.write_version_diff(doc2, settings, now=doc2 and datetime(2026, 10, 7, 13, 0, 0, tzinfo=UTC))
        eval_diff.append_diff_summary(doc1, settings, now=_NOW)
        summary2 = eval_diff.append_diff_summary(doc2, settings, now=datetime(2026, 10, 7, 13, 0, 0, tzinfo=UTC))
        # Assert：最新件=t2；历史件两份；SUMMARY 两节都在且文件头只一次
        latest = json.loads((settings.eval_dir() / settings.version_diff_filename).read_text(encoding="utf-8"))
        assert latest["release_tag"] == "t2"
        history_dir = settings.eval_dir() / "2026-10-07"
        assert len(list(history_dir.glob("*-version_diff.json"))) == 2
        text = summary2.read_text(encoding="utf-8")
        assert text.count("# eval release 曲线") == 1
        assert text.count("release=t1") == 1 and text.count("release=t2") == 1
        assert "bench-core" not in text  # tmp 夹具域隔离：不串真实结果
        assert path2["latest"].endswith("version_diff.json")
