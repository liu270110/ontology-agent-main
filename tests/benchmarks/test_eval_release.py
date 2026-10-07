"""eval release dashboard 单测（docs/Agent/17 §2 批次 B 第 1/3 项）：聚合+缺失容错+健康标记。

锁定四件事：
1. dashboard 聚合四套件（各 suite 指标从结果 JSON 原样透传，键=<来源>.<字段>）；
2. 缺失容错：suite 目录缺失/结果损坏 → 健康标记 partial，dashboard 照常产出不抛异常；
3. 健康标记分级：manifest assert_failed>0、场景缺格、intent 档指标缺失 → partial；
4. market_reference：citation_only 三档指针 + red_line 透传（读真实引用件，只读不并列）。

AAA 注：Arrange/Act/Assert 中文注释逐段标注；夹具合成 tmp 结果目录；引用件用仓内真实
public_leaderboards.json（与 test_rag_leaderboard_citations 同一事实源）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.suites.eval import release as eval_release
from benchmarks.suites.eval.config import EvalReleaseSettings
from benchmarks.suites.eval.runs import discover_runs, load_run, suite_health

_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _settings(tmp_path: Path) -> EvalReleaseSettings:
    """Arrange 公共件：tmp 结果目录 + 真实榜单引用件 + 夹具规模的期望场景数（agent-core=2）。"""
    return EvalReleaseSettings(
        results_dir=tmp_path / "benchmarks" / "results",
        citations_path=_REPO_ROOT / "benchmarks/suites/rag/leaderboard_citations/public_leaderboards.json",
        expected_scenario_counts={"agent-core": 2, "ontology-scale": 3},
    )


def _make_agent_core_run(
    tmp_path: Path,
    clock: str,
    tag: str,
    *,
    started_at: str,
    finished_at: str,
    mutex: float,
    status: str = "ok",
    status_counts: dict | None = None,
) -> None:
    """Arrange：合成一次 agent-core run（两场景 + manifest，状态可注入）。"""
    date_dir = tmp_path / "benchmarks" / "results" / "agent-core" / "2026-10-07"
    date_dir.mkdir(parents=True, exist_ok=True)
    for scenario, metric_key, value in (
        ("session_mutex_rate", "session_mutex_rate", mutex),
        ("cross_tenant_leak", "reject_rate", 1.0),
    ):
        (date_dir / f"{clock}-{scenario}.json").write_text(
            json.dumps(
                {
                    "suite": "agent-core",
                    "scenario": scenario,
                    "smoke": True,
                    "started_at": started_at,
                    "params": {"smoke": True},
                    "metrics": {metric_key: value},
                    "status": status,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    (date_dir / f"{clock}-manifest.json").write_text(
        json.dumps(
            {
                "suite": "agent-core",
                "smoke": True,
                "tag": tag,
                "finished_at": finished_at,
                "status_counts": status_counts or {"ok": 2, "assert_failed": 0, "error": 0},
                "env": {"commit": "c" * 40, "branch": "feature/x", "python": "3.13.0", "platform": "Windows-11"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _make_rag_run(tmp_path: Path, tag: str, *, recall: float, with_metrics: bool = True) -> None:
    """Arrange：合成一次 rag run（三 harness 六维可注入）。"""
    date_dir = tmp_path / "benchmarks" / "results" / "rag" / "2026-10-07"
    date_dir.mkdir(parents=True, exist_ok=True)
    harnesses = {}
    for name in ("ours_kb", "naive_bm25", "naive_vector"):
        harnesses[name] = {
            "metrics": (
                {
                    "recall_at_k": recall,
                    "mrr": recall,
                    "faithfulness": 1.0,
                    "latency_p50_ms": 100.0,
                    "latency_p95_ms": 200.0,
                    "cost_per_query_tokens": 500.0,
                }
                if with_metrics
                else {}
            )
        }
    (date_dir / f"run-100000-{tag}.json").write_text(
        json.dumps(
            {
                "suite": "rag",
                "tag": tag,
                "date": "2026-10-07T10:00:00",
                "env": {"commit": "d" * 40, "corpus_version": "v0", "corpus_sha256": "ab" * 32},
                "harnesses": harnesses,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- ① dashboard 聚合


def _make_rag_summary_with_qualitative(tmp_path: Path) -> None:
    """Arrange：合成 rag SUMMARY.md 人审定性位置节（market_reference 定性文本的既有人审出处）。"""
    rag_dir = tmp_path / "benchmarks" / "results" / "rag"
    rag_dir.mkdir(parents=True, exist_ok=True)
    (rag_dir / "SUMMARY.md").write_text(
        "# rag suite 结果曲线\n\n"
        "### 我们的定性位置（非并列结论）\n\n"
        "我们 naive_bm25 在自有电力语料 recall@5=1.0，BEIR 论文 BM25 十八集零样本平均 nDCG@10≈0.42"
        "——仅量级合理性参考，不构成任何名次结论。\n",
        encoding="utf-8",
    )


class TestBuildDashboard:
    """build_dashboard：四套件透传聚合 + 环境指纹 + 市场引用块。"""

    def test_四套件聚合_指标透传且健康ok(self, tmp_path):
        # Arrange：agent-core（scenario 型）+ rag（single 型）有真实形态结果；intent/ontology-scale 留空
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v9",
            started_at="2026-10-07T00:00:00+00:00",
            finished_at="2026-10-07T00:00:01+00:00",
            mutex=1.0,
        )
        _make_rag_run(tmp_path, "v9", recall=0.975)
        settings = _settings(tmp_path)
        # Act
        doc = eval_release.build_dashboard(settings, tag="t-dashboard", now=_NOW)
        # Assert：schema/tag/透传键与健康
        assert doc["schema"] == "eval-release-dashboard/v1"
        assert doc["tag"] == "t-dashboard"
        ac = doc["suites"]["agent-core"]
        assert ac["health"] == "ok"
        assert ac["metrics"]["session_mutex_rate.session_mutex_rate"] == 1.0
        assert ac["metrics"]["cross_tenant_leak.reject_rate"] == 1.0
        rag = doc["suites"]["rag"]
        assert rag["health"] == "ok"
        assert rag["metrics"]["ours_kb.recall_at_k"] == 0.975
        assert rag["metrics"]["naive_bm25.latency_p50_ms"] == 100.0
        # 缺席套件：partial 且 metrics 空，不抛异常
        for missing in ("intent", "ontology-scale"):
            assert doc["suites"][missing]["health"] == "partial"
            assert doc["suites"][missing]["health_reason"] == "no_results_found"
            assert doc["suites"][missing]["metrics"] == {}

    def test_环境指纹按套件分列且白名单透传(self, tmp_path):
        # Arrange：agent-core env 含 commit/branch/python/platform + 非白名单键
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v9",
            started_at="2026-10-07T00:00:00+00:00",
            finished_at="2026-10-07T00:00:01+00:00",
            mutex=1.0,
        )
        settings = _settings(tmp_path)
        # Act
        doc = eval_release.build_dashboard(settings, tag="t", now=_NOW)
        # Assert：白名单键透传，缺失键不造值
        fp = doc["env_fingerprint"]["suites"]["agent-core"]
        assert fp["commit"] == "c" * 40
        assert fp["branch"] == "feature/x"
        assert "model" not in fp  # 夹具 env 无 model → 不造
        assert "corpus_version" not in fp  # agent-core env 无语料键

    def test_market_reference_三档指针与红线透传(self, tmp_path):
        # Arrange：空结果目录 + rag SUMMARY 人审定性位置节
        _make_rag_summary_with_qualitative(tmp_path)
        settings = _settings(tmp_path)
        # Act
        doc = eval_release.build_dashboard(settings, tag="t", now=_NOW)
        market = doc["market_reference"]
        # Assert：真实引用件 → 三档各有指针；A/B/C 各至少 1 条；red_line 随件透传
        assert market["nature"] == "citation_only"
        assert market["red_line"] and "禁止直接并列" in market["red_line"]
        for tier in ("A", "B", "C"):
            assert market["tiers"][tier]["pointers"], f"{tier} 档指针为空"
            assert market["tiers"][tier]["definition"]
        # 定性位置引用自 rag SUMMARY 人审节（节正文原文；节标题按切分规则不在正文内）
        assert market["qualitative_position"]["source"].endswith("SUMMARY.md")
        assert "量级合理性参考" in market["qualitative_position"]["A"]
        assert market["qualitative_position"]["B"] == market["qualitative_position"]["A"]

    def test_market_reference_引用件缺失降级不抛(self, tmp_path):
        # Arrange：citations_path 指向不存在文件
        settings = EvalReleaseSettings(
            results_dir=tmp_path / "benchmarks" / "results",
            citations_path=tmp_path / "no" / "such.json",
        )
        # Act / Assert：dashboard 照常产出，market 如实 unavailable
        doc = eval_release.build_dashboard(settings, tag="t", now=_NOW)
        assert doc["market_reference"]["status"] == "unavailable"
        assert "citations_file_missing" in doc["market_reference"]["reason"]


# ---------------------------------------------------------------- ② 健康标记分级


class TestSuiteHealth:
    """suite_health：结果缺失/部分跑 → partial；全 ok → ok。"""

    def test_场景非ok_标partial(self, tmp_path):
        # Arrange：一个场景 assert_failed
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v9",
            started_at="2026-10-07T00:00:00+00:00",
            finished_at="2026-10-07T00:00:01+00:00",
            mutex=1.0,
            status="assert_failed",
        )
        settings = _settings(tmp_path)
        run = discover_runs("agent-core", settings)[-1]
        loaded = load_run(run)
        # Act
        health, reason = suite_health("agent-core", run, loaded, settings)
        # Assert：夹具 status 注入两场景 → 两个场景都列名
        assert (health, reason) == ("partial", "scenario_not_ok=['cross_tenant_leak', 'session_mutex_rate']")

    def test_manifest计数非零_标partial(self, tmp_path):
        # Arrange：场景全 ok 但 manifest 记 assert_failed=1（两处记录不一致以 manifest 为准）
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v9",
            started_at="2026-10-07T00:00:00+00:00",
            finished_at="2026-10-07T00:00:01+00:00",
            mutex=1.0,
            status_counts={"ok": 1, "assert_failed": 1, "error": 0},
        )
        settings = _settings(tmp_path)
        run = discover_runs("agent-core", settings)[-1]
        loaded = load_run(run)
        # Act / Assert
        assert suite_health("agent-core", run, loaded, settings) == ("partial", "manifest_status_counts_nonzero")

    def test_场景数不足期望_标partial(self, tmp_path):
        # Arrange：只落 1/2 场景（无 manifest，进程中断形态）
        date_dir = tmp_path / "benchmarks" / "results" / "agent-core" / "2026-10-07"
        date_dir.mkdir(parents=True)
        (date_dir / "080000-session_mutex_rate.json").write_text(
            json.dumps(
                {
                    "suite": "agent-core",
                    "scenario": "session_mutex_rate",
                    "smoke": True,
                    "started_at": "2026-10-07T00:00:00+00:00",
                    "params": {},
                    "metrics": {"session_mutex_rate": 1.0},
                    "status": "ok",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        settings = _settings(tmp_path)
        run = discover_runs("agent-core", settings)[-1]
        loaded = load_run(run)
        # Assert：incomplete_run=1/2（夹具期望 agent-core=2）
        assert suite_health("agent-core", run, loaded, settings) == ("partial", "incomplete_run=1/2")

    def test_rag_harness指标缺失_标partial(self, tmp_path):
        # Arrange：rag run 存在但 harness metrics 空（partial 落盘形态）
        _make_rag_run(tmp_path, "v9", recall=1.0, with_metrics=False)
        settings = _settings(tmp_path)
        run = discover_runs("rag", settings)[-1]
        loaded = load_run(run)
        # Act / Assert
        health, reason = suite_health("rag", run, loaded, settings)
        assert health == "partial"
        assert "harness_metrics_missing" in reason

    def test_无run_标partial_no_results_found(self, tmp_path):
        # Arrange：空结果目录
        settings = _settings(tmp_path)
        # Act / Assert：缺失结果同样落 partial（ask 口径：结果缺失/部分跑 → partial）
        assert suite_health("rag", None, None, settings) == ("partial", "no_results_found")

    def test_损坏场景件不入组_发现不炸(self, tmp_path):
        # Arrange：合法 run + 同目录半写损坏件（JSON 截断）
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v9",
            started_at="2026-10-07T00:00:00+00:00",
            finished_at="2026-10-07T00:00:01+00:00",
            mutex=1.0,
        )
        date_dir = tmp_path / "benchmarks" / "results" / "agent-core" / "2026-10-07"
        (date_dir / "090000-broken_scenario.json").write_text('{"suite": "agent-core", "scen', encoding="utf-8")
        settings = _settings(tmp_path)
        # Act：发现+装载最新 run 全程不抛
        runs = discover_runs("agent-core", settings)
        loaded = load_run(runs[-1])
        # Assert：损坏件不在场景集里，合法两场景齐
        assert "broken_scenario" not in (loaded.get("scenarios") or {})
        assert set(loaded["scenarios"]) == {"session_mutex_rate", "cross_tenant_leak"}


# ---------------------------------------------------------------- ③ 落盘与 SUMMARY 追加、run.py 入口编排


class TestWriteAndOrchestration:
    """write_dashboard / run_release_eval（run.py --release-eval 背后编排）。"""

    def test_写盘_最新件加历史件(self, tmp_path):
        # Arrange
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v9",
            started_at="2026-10-07T00:00:00+00:00",
            finished_at="2026-10-07T00:00:01+00:00",
            mutex=1.0,
        )
        settings = _settings(tmp_path)
        doc = eval_release.build_dashboard(settings, tag="t1", now=_NOW)
        # Act
        paths = eval_release.write_dashboard(doc, settings, now=_NOW)
        # Assert
        latest = json.loads(Path(paths["latest"]).read_text(encoding="utf-8"))
        assert latest["tag"] == "t1"
        assert Path(paths["history"]).is_file()
        assert (settings.eval_dir() / "2026-10-07" / "120000-dashboard.json").is_file()

    def test_release编排_dashboard与diff双产物加SUMMARY双节(self, tmp_path):
        # Arrange：agent-core 两 tag（diff 可演示）+ rag 一 run
        _make_agent_core_run(
            tmp_path,
            "080000",
            "v1",
            started_at="2026-10-07T00:00:00+00:00",
            finished_at="2026-10-07T00:00:01+00:00",
            mutex=1.0,
        )
        _make_agent_core_run(
            tmp_path,
            "090000",
            "v2",
            started_at="2026-10-07T01:00:00+00:00",
            finished_at="2026-10-07T01:00:01+00:00",
            mutex=1.0,
        )
        _make_rag_run(tmp_path, "v1", recall=0.9)
        settings = _settings(tmp_path)
        # Act
        report = eval_release.run_release_eval(settings, tag="t1", now=_NOW)
        # Assert：两产物落盘 + SUMMARY 两节追加 + 摘要可序列化
        assert Path(report["dashboard"]["latest"]).is_file()
        assert Path(report["version_diff"]["latest"]).is_file()
        summary = (settings.eval_dir() / settings.summary_filename).read_text(encoding="utf-8")
        assert "release-eval tag=t1" in summary
        assert "release=t1 version-diff" in summary
        assert report["diff"]["agent-core"]["status"] == "ok"
        assert report["diff"]["agent-core"]["pair"] == "v1 → v2"
        assert report["diff"]["rag"]["status"] == "no_baseline"

    def test_run入口_release_eval无suite可用且非法组合返回2(self, tmp_path, monkeypatch, capsys):
        # Arrange：经 env 把结果目录指到 tmp（Settings 注入面），跑真实 run.main
        import benchmarks.run as bench_run

        monkeypatch.setenv("BENCH_EVAL_RESULTS_DIR", str(tmp_path / "benchmarks" / "results"))
        monkeypatch.chdir(tmp_path)
        # Act / Assert ①：--release-eval 不带 --suite 可解析
        args = bench_run.build_parser().parse_args(["--release-eval", "--tag", "t-env"])
        assert args.release_eval is True
        assert args.suite is None
        # Act / Assert ②：--diff-prev 不成对 → 退出码 2 且提示上屏
        code = bench_run.main(["--release-eval", "--diff-prev", "x"])
        assert code == 2
        assert "--diff-prev/--diff-curr 须成对" in capsys.readouterr().err
