# tests/benchmarks/test_flywheel_harvest.py
"""飞轮转化环 harvest 管道测试（docs/Agent/19 §5 批 F2/B6）。

覆盖面（ask 指定五组）：脱敏函数（UUID/邮箱/IP/路径四类+保守性+同词同号）/
正负例分类逻辑/proposed 草稿过 runner.load_scenario_specs 同款 schema 校验（且不进
正式套件）/orsi 建议形状（face=O4 track=shortgap）/空 feedback 窗口零产出；
另含：新指标口径 task_outcome_user_reported 纯函数、TableMissing 协议兼容零产出、
CLI 冒烟、PG 真库协议用例（表在→读，列缺→降级，表缺→TableMissing——F1 合入自动生效证）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用；test_orsi_link_pg 同款）
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from benchmarks.flywheel.harvest import (
    KIND_GOLDEN,
    KIND_REGRESSION,
    KIND_SKIP,
    FeedbackRow,
    FlywheelSettings,
    HarvestCandidate,
    JsonFeedbackReader,
    PgFeedbackReader,
    TableMissingError,
    build_asserts,
    build_orsi_suggestions,
    classify_feedback,
    load_agent_core_runner,
    main,
    parse_since,
    run_harvest,
    sanitize_text,
    scenario_name,
    validate_drafts,
)
from services.platform.config import Settings
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

_SINCE = datetime(2026, 10, 7, tzinfo=UTC)


def _metrics():
    """agent-core metrics 模块（目录名含连字符不可作包名——runner 同款文件位加载）。"""
    return load_agent_core_runner().metrics


def _flywheel_settings(tmp_path: Path) -> FlywheelSettings:
    """隔离落点：草稿队列与结果根都指 tmp（不污染仓库 proposed/ 与 results/）。"""
    return FlywheelSettings(
        proposed_dir=tmp_path / "proposed",
        results_root=tmp_path / "results",
    )


def _write_stub(tmp_path: Path, rows: list[dict]) -> Path:
    stub = tmp_path / "feedback_stub.json"
    stub.write_text(json.dumps({"feedback": rows}, ensure_ascii=False), encoding="utf-8")
    return stub


# ---------------------------------------------------------------------------
# ① 脱敏函数（UUID/邮箱/IP/路径四类 + 保守性 + 同词同号）
# ---------------------------------------------------------------------------


def test_脱敏_四类专有名词替换与计数() -> None:
    # Arrange：覆盖四类的任务文本（含 CJK 邻接形态与路径尾标点）
    text = (
        "分析线路 03de6c4b-ca6e-4f8e-9f3c-1a2b3c4d5e6f 故障，联系 admin@example.com，"
        "网关地址192.168.1.10，数据在 E:\\grid\\data\\feeders.csv，日志见 /var/log/oa/run.log。"
    )
    # Act
    sanitized, counts = sanitize_text(text)
    # Assert：四类全部替换为占位符；原文专有名词不残留；分类计数各 1
    assert "03de6c4b" not in sanitized and "admin@example.com" not in sanitized
    assert "192.168.1.10" not in sanitized and "E:\\grid" not in sanitized and "/var/log/oa" not in sanitized
    assert "⟪USER_" in sanitized
    assert counts == {"path": 2, "email": 1, "uuid": 1, "ip": 1}
    # 路径尾标点（句号）保留不丢
    assert sanitized.endswith("。")


def test_脱敏_同一原词同号占位与逐次计数() -> None:
    # Arrange：同一邮箱出现两次
    text = "发给 admin@example.com，再抄送 admin@example.com 归档"
    # Act
    sanitized, counts = sanitize_text(text)
    # Assert：同词同号（共指结构保留），替换计数按发生次数
    assert sanitized.count("⟪USER_1⟫") == 2 and "⟪USER_2⟫" not in sanitized
    assert counts["email"] == 2


def test_脱敏_保守性_版本号多段点串与端口不误伤() -> None:
    # Arrange：形似 IP 的非 IP 串与带端口的真 IP
    text = "内核版本 v1.2.3.4，多段点 1.2.3.4.5，越界段 999.1.1.1，网关 10.0.0.8:8000"
    # Act
    sanitized, counts = sanitize_text(text)
    # Assert：三 类保守形态原样保留；真 IP 替换且端口保留
    assert "v1.2.3.4" in sanitized and "1.2.3.4.5" in sanitized and "999.1.1.1" in sanitized
    assert "10.0.0.8" not in sanitized and ":8000" in sanitized
    assert counts == {"path": 0, "email": 0, "uuid": 0, "ip": 1}


# ---------------------------------------------------------------------------
# ② 新指标口径（metrics.py ⑦ 纯函数）与正负例分类逻辑
# ---------------------------------------------------------------------------


def test_新口径_task_outcome_user_reported纯函数() -> None:
    # Arrange & Act & Assert：全 completed 才 True；failed/partial/未知只计数；空样本 False
    metric = _metrics().task_outcome_user_reported
    assert metric(["completed", "completed"])["task_outcome_user_reported"] is True
    mixed = metric(["completed", "failed", "partial", "weird", "completed"])
    assert mixed["task_outcome_user_reported"] is False
    assert mixed == {
        "reports_total": 4,
        "completed_reports": 2,
        "failed_reports": 1,
        "partial_reports": 1,
        "unknown_reports": 1,
        "task_outcome_user_reported": False,
    }
    assert metric([])["task_outcome_user_reported"] is False


def test_分类_正例golden与负例regression及跳过() -> None:
    # Arrange：§5 全部分支形态
    neg_tags = FlywheelSettings().negative_tags
    completed_clean = FeedbackRow(run_id="r1", outcome="completed", tags=[], correction_text=None)
    completed_with_correction = FeedbackRow(run_id="r2", outcome="completed", correction_text="结果不对")
    completed_with_neg_tag = FeedbackRow(run_id="r3", outcome="completed", tags=["unhelpful"])
    failed = FeedbackRow(run_id="r4", outcome="failed")
    partial = FeedbackRow(run_id="r5", outcome="partial", tags=["wrong"])
    unknown = FeedbackRow(run_id="r6", outcome="meh")
    missing = FeedbackRow(run_id="r7", outcome=None)
    # Act & Assert：正例/负例/纠错优先/未知跳过
    assert classify_feedback(completed_clean, negative_tags=neg_tags) == (KIND_GOLDEN, "completed_clean")
    # 纠错优先于完成态——用户否定是最强信号（19 §5）
    assert classify_feedback(completed_with_correction, negative_tags=neg_tags)[0] == KIND_REGRESSION
    assert classify_feedback(completed_with_neg_tag, negative_tags=neg_tags)[0] == KIND_REGRESSION
    assert classify_feedback(failed, negative_tags=neg_tags) == (KIND_REGRESSION, "outcome=failed")
    assert classify_feedback(partial, negative_tags=neg_tags)[0] == KIND_REGRESSION
    assert classify_feedback(unknown, negative_tags=neg_tags)[0] == KIND_SKIP
    assert classify_feedback(missing, negative_tags=neg_tags)[0] == KIND_SKIP
    # 断言口径：golden 只引新指标；regression 追加无效重试有界（两键均在白名单）
    assert build_asserts(KIND_GOLDEN) == [{"metric": "task_outcome_user_reported", "op": "==", "value": True}]
    assert [a["metric"] for a in build_asserts(KIND_REGRESSION)] == [
        "task_outcome_user_reported",
        "invalid_retry_count",
    ]


def test_parse_since_日期归一UTC() -> None:
    # Arrange & Act & Assert：日期串补 UTC；带时区保持
    assert parse_since("2026-10-07") == _SINCE
    assert parse_since("2026-10-07T08:00:00+00:00") == datetime(2026, 10, 7, 8, tzinfo=UTC)


# ---------------------------------------------------------------------------
# ③ 管道：草稿落 proposed 过 schema 校验（且不进正式套件）+ orsi 建议形状
# ---------------------------------------------------------------------------


async def test_管道_正负例各一_草稿过schema校验且不进正式套件(tmp_path: Path) -> None:
    # Arrange：stub 反馈（正例含 IP+Windows 路径；负例纠错含邮箱+POSIX 路径）
    stub = _write_stub(
        tmp_path,
        [
            {
                "run_id": "11111111-1111-4111-8111-111111111111",
                "outcome": "completed",
                "tags": [],
                "correction_text": None,
                "task_prompt": "汇总 10.0.0.8 网段 E:\\grid\\data\\feeders.csv 的停电工单",
                "created_at": "2026-10-07T08:00:00+00:00",
            },
            {
                "run_id": "22222222-2222-4222-8222-222222222222",
                "outcome": "failed",
                "tags": [],
                "correction_text": "结果错了：应查 ops@corp.cn 名下 /var/data/tickets.json",
                "task_prompt": "汇总停电工单",
                "created_at": "2026-10-07T09:00:00+00:00",
            },
        ],
    )
    settings = _flywheel_settings(tmp_path)
    # Act
    report = await run_harvest(settings=settings, since=_SINCE, source=JsonFeedbackReader(stub))
    # Assert：分类与计数
    counts = report["counts"]
    assert (counts["feedback_total"], counts["golden"], counts["regression"]) == (2, 1, 1)
    assert (counts["drafts_written"], counts["drafts_validated"]) == (2, 2)
    assert counts["sanitization"]["ip"] == 1 and counts["sanitization"]["path"] >= 2
    assert counts["sanitization"]["email"] == 1 and counts["sanitization"]["uuid"] == 0
    # 草稿内容：脱敏占位 + 断言口径
    drafts = sorted((settings.proposed_dir).glob("*.yaml"))
    assert len(drafts) == 2
    golden_draft = next(p for p in drafts if "golden" in p.name)
    regression_draft = next(p for p in drafts if "regression" in p.name)
    golden_text = golden_draft.read_text(encoding="utf-8")
    regression_text = regression_draft.read_text(encoding="utf-8")
    assert "⟪USER_" in golden_text and "10.0.0.8" not in golden_text and "E:\\grid" not in golden_text
    assert "⟪USER_" in regression_text and "ops@corp.cn" not in regression_text
    assert "user_correction" in regression_text
    # schema 复检：runner.load_scenario_specs 同款白名单校验整目录通过
    runner = load_agent_core_runner()
    specs = runner.load_scenario_specs(settings.proposed_dir)
    assert len(specs) == 2 and validate_drafts(settings.proposed_dir, list(specs)) == 2
    golden_spec = next(s for s in specs.values() if ":golden:" in s.scenario)
    regression_spec = next(s for s in specs.values() if ":regression:" in s.scenario)
    assert golden_spec.params["expected_outcome"] == "completed"
    assert {a.metric for a in regression_spec.asserts} == {"task_outcome_user_reported", "invalid_retry_count"}
    # 防污染隔离：正式套件 loader 非递归 glob——proposed 草稿不进正式套件
    formal = runner.load_scenario_specs(Path(__file__).resolve().parents[2] / "benchmarks/suites/agent-core/scenarios")
    assert formal and all(not name.startswith("flywheel") for name in formal)
    # 产物：harvest.json + SUMMARY.md 落档
    out_dir = settings.results_root / "flywheel" / "2026-10-07"
    assert (out_dir / "harvest.json").is_file() and (out_dir / "SUMMARY.md").is_file()


def test_orsi建议形状_O4_shortgap_candidate且只建议不注册(tmp_path: Path) -> None:
    # Arrange：两个负例候选（草稿路径先落 tmp 再造相对 uri）
    candidates = [
        HarvestCandidate(
            row=FeedbackRow(run_id=f"run-{index}", outcome="failed"),
            kind=KIND_REGRESSION,
            reason="outcome=failed",
            short=f"short{index}",
            scenario=scenario_name(KIND_REGRESSION, f"short{index}"),
            task_prompt_sanitized="x",
            correction_sanitized=None,
            sanitize_counts={"uuid": 0, "email": 0, "ip": 0, "path": 0},
        )
        for index in range(2)
    ]
    draft_rel_uri = {c.scenario: f"tmp/{c.kind}-{c.short}.yaml" for c in candidates}
    # Act
    suggestions = build_orsi_suggestions(candidates, draft_rel_uri=draft_rel_uri, repo_root=tmp_path)
    # Assert：形状（orsi_link 建议形态同构）+ O4/shortgap/候选非成品
    assert len(suggestions) == 2
    for suggestion in suggestions:
        assert suggestion["suggestion_type"] == "orsi_capability_registration"
        assert suggestion["auto_registered"] is False  # 只建议不注册（宪法 3）
        proposal = suggestion["proposal"]
        assert proposal["name"].startswith("quality_regression:flywheel.")
        assert proposal["face"] == "O4"
        assert proposal["status"] == "candidate"
        assert proposal["source_face_track"] == "shortgap"
        assert proposal["source_channel"] == "L0"
        evidence = suggestion["evidence"]
        assert evidence["run_status"] == "proposed" and evidence["key_metrics"] == {}
        assert evidence["benchmark_ref"].startswith("docs/Agent/19")
        assert len(suggestion["capability_fingerprint"]) == 64
        assert suggestion["fingerprint_note"] and suggestion["rationale"]
    # 指纹互异（不同场景不同指纹）；空负例 → 零建议
    assert suggestions[0]["capability_fingerprint"] != suggestions[1]["capability_fingerprint"]
    assert build_orsi_suggestions([], draft_rel_uri={}, repo_root=tmp_path) == []


# ---------------------------------------------------------------------------
# ④ 空 feedback 窗口零产出 + TableMissing 协议兼容零产出
# ---------------------------------------------------------------------------


async def test_空feedback窗口零产出但产物照常落档(tmp_path: Path) -> None:
    # Arrange：空 stub 窗口
    stub = _write_stub(tmp_path, [])
    settings = _flywheel_settings(tmp_path)
    # Act
    report = await run_harvest(settings=settings, since=_SINCE, source=JsonFeedbackReader(stub))
    # Assert：全零计数、零草稿、零建议；产物（harvest.json/SUMMARY.md）照常落档
    assert report["counts"] == {
        "feedback_total": 0,
        "golden": 0,
        "regression": 0,
        "skipped_unknown_outcome": 0,
        "skipped_empty_signal": 0,
        "drafts_written": 0,
        "drafts_validated": 0,
        "sanitization": {"uuid": 0, "email": 0, "ip": 0, "path": 0, "total": 0},
    }
    assert report["candidates"] == [] and report["orsi_suggestions"] == []
    out_dir = settings.results_root / "flywheel" / "2026-10-07"
    assert (out_dir / "harvest.json").is_file() and (out_dir / "SUMMARY.md").is_file()
    assert list(settings.proposed_dir.glob("*.yaml")) == []


async def test_TableMissing_协议兼容零产出且如实落注(tmp_path: Path) -> None:
    # Arrange：F1 未合入形态的源（表缺失信号）
    class MissingSource:
        def describe(self) -> str:
            return "pg:session_feedback"

        async def read(self, since: datetime) -> list[FeedbackRow]:
            raise TableMissingError("session_feedback 表不存在——采集批（F1）未合入")

    settings = _flywheel_settings(tmp_path)
    # Act
    report = await run_harvest(settings=settings, since=_SINCE, source=MissingSource())  # type: ignore[arg-type]
    # Assert：零产出不臆造；note 如实落档；SUMMARY 带注记
    assert report["counts"]["feedback_total"] == 0 and report["counts"]["drafts_written"] == 0
    assert report["orsi_suggestions"] == [] and report["candidates"] == []
    assert "F1" in (report["note"] or "")
    summary = (settings.results_root / "flywheel" / "2026-10-07" / "SUMMARY.md").read_text(encoding="utf-8")
    assert "note:" in summary


async def test_无任务信号的候选被跳过计数(tmp_path: Path) -> None:
    # Arrange：completed 但无任务文本无纠错 → 无信号；outcome 非法 → 未知
    stub = _write_stub(
        tmp_path,
        [
            {"run_id": "r-none", "outcome": "completed", "tags": []},
            {"run_id": "r-bad", "outcome": "meh"},
        ],
    )
    settings = _flywheel_settings(tmp_path)
    # Act
    report = await run_harvest(settings=settings, since=_SINCE, source=JsonFeedbackReader(stub))
    # Assert：跳过计数如实，不产草稿
    assert report["counts"]["skipped_empty_signal"] == 1
    assert report["counts"]["skipped_unknown_outcome"] == 1
    assert report["counts"]["drafts_written"] == 0


# ---------------------------------------------------------------------------
# ⑤ CLI 冒烟（env 注入隔离落点）
# ---------------------------------------------------------------------------


def test_CLI_stub全链冒烟(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：stub + env 注入 tmp 落点（Settings 读取面同生产）
    stub = _write_stub(
        tmp_path,
        [
            {
                "run_id": "33333333-3333-4333-8333-333333333333",
                "outcome": "failed",
                "tags": ["wrong"],
                "correction_text": "断网时应该提示，联系 support@corp.cn，日志 /var/log/oa/x.log",
                "task_prompt": "断网重连",
                "created_at": "2026-10-07T10:00:00+00:00",
            }
        ],
    )
    monkeypatch.setenv("BENCH_FLYWHEEL_PROPOSED_DIR", str(tmp_path / "proposed"))
    monkeypatch.setenv("BENCH_FLYWHEEL_RESULTS_ROOT", str(tmp_path / "results"))
    # Act：CLI 全链（parse→read→classify→sanitize→draft→validate→orsi→artifacts）
    rc = main(["--since", "2026-10-07", "--feedback-json", str(stub)])
    # Assert：退出码 0；草稿与建议与产物齐备
    assert rc == 0
    drafts = list((tmp_path / "proposed").glob("flywheel-regression-*.yaml"))
    assert len(drafts) == 1
    report = json.loads((tmp_path / "results" / "flywheel" / "2026-10-07" / "harvest.json").read_text(encoding="utf-8"))
    assert report["counts"]["regression"] == 1 and len(report["orsi_suggestions"]) == 1
    assert report["orsi_suggestions"][0]["proposal"]["source_face_track"] == "shortgap"


# ---------------------------------------------------------------------------
# ⑥ PG 真库协议用例（一次性私库；PG 不可达自动跳过——F1 合入自动生效证）
# ---------------------------------------------------------------------------


@pytest.fixture
async def pg_env(tmp_path: Path) -> AsyncIterator[Settings]:
    """一次性私库：探活→建库→create_all→手工建 session_feedback（§5 字段）→yield→DROP。"""
    base = Settings()
    if not await probe_pg(base.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过 flywheel 真库协议用例")
    test_dsn = await create_test_database(base.pg_dsn)
    dbname = test_dsn.rsplit("/", 1)[1]
    settings = Settings(jwt_secret="flywheel-pg-test-secret-0123456789abcdef", deploy_profile="lite", pg_db=dbname)
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(settings.pg_dsn)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE session_feedback ("
                    " id text PRIMARY KEY,"
                    " run_id uuid NOT NULL,"
                    " outcome text,"
                    " tags jsonb NOT NULL DEFAULT '[]'::jsonb,"
                    " correction_text text,"
                    " task_prompt text,"
                    " created_at timestamptz NOT NULL DEFAULT now())"
                )
            )
        yield settings
    finally:
        await engine.dispose()
        with suppress(Exception):
            await drop_test_database(test_dsn)


async def test_PG直读_全字段_列缺失降级与表缺失信号(pg_env: Settings) -> None:
    # Arrange：§5 形态两行（一正一负，专有名词齐四类）
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    since = datetime.now(UTC) - timedelta(hours=1)
    run_golden, run_regression = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(pg_env.pg_dsn)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO session_feedback (id, run_id, outcome, tags, correction_text, task_prompt)"
                " VALUES (:id, :run_id, :outcome, CAST(:tags AS jsonb), :correction, :prompt)"
            ),
            [
                {
                    "id": "f1",
                    "run_id": str(run_golden),
                    "outcome": "completed",
                    "tags": "[]",
                    "correction": None,
                    "prompt": "分析 10.0.0.8 与 E:\\grid\\a.csv，联系 a@b.com，"
                    "run 03de6c4b-ca6e-4f8e-9f3c-1a2b3c4d5e6f",
                },
                {
                    "id": "f2",
                    "run_id": str(run_regression),
                    "outcome": "failed",
                    "tags": '["wrong"]',
                    "correction": "路径应为 /var/data/t.json",
                    "prompt": "汇总工单",
                },
            ],
        )
    await engine.dispose()
    reader = PgFeedbackReader(pg_env)
    # Act ①：全字段读
    rows = await reader.read(since)
    # Assert ①：协议字段映射（tags JSONB→list）
    assert len(rows) == 2
    by_run = {row.run_id: row for row in rows}
    assert by_run[str(run_golden)].outcome == "completed" and by_run[str(run_golden)].task_prompt
    assert by_run[str(run_regression)].tags == ["wrong"]
    # Act ②：task_prompt 列缺失（F1 首版形态）→ 最小字段集降级重查
    engine = create_async_engine(pg_env.pg_dsn)
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE session_feedback DROP COLUMN task_prompt"))
    await engine.dispose()
    rows_minimal = await reader.read(since)
    # Assert ②：§5 四字段仍在，协议扩展位为 None（不臆造）
    assert len(rows_minimal) == 2 and all(row.task_prompt is None for row in rows_minimal)
    # Act ③：整表缺失（回滚模拟）→ TableMissing 信号
    engine = create_async_engine(pg_env.pg_dsn)
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE session_feedback"))
    await engine.dispose()
    with pytest.raises(TableMissingError, match="F1"):
        await reader.read(since)
