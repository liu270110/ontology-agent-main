# tests/kb/test_agentic_golden.py
"""golden agentic 用例集（AgenticRAG优化方案 §7 待办①：判别/评级阈值 PoC；纯内存零 PG 依赖）。

金标语料本体=services/kb/business/agentic_calibration.py（业务层评估资产，同 retrieval_eval
惯例；tests→services 单向 import 合规）。本文件逐条断言金标期望并钉住规模门禁：

- 判别金标 38 条（检索必需 12 / 无需检索 14 / 边界灰区 12）逐条断言 decide()；
- 评级金标 22 条（命中/零命中/低分/降级单路/含图路 各≥4 + span 门禁 3）逐条断言 grade()；
  场景经 seed_class 锚定种子目录（load_seed_catalog 真装载校验标签存在——判定依据可回指）；
- 词表覆盖：寒暄词面逐词正向（含全部词面项可达性——标定前「哈喽」因尾字剥除不可达即被
  此用例抓获）+ 词面×语气尾字全积变体 + 尾字逐字符剥除 + 「吗」不剥探针 + 形近负向；
- 标定裁决探针：哈喽（修复项）/ 谢谢喽（弃喽变体保守放行）/ 阈值扫描最优档复现
  （0.05~0.95 步进 0.05，最优档 {0.50, 0.55}，常量维持 0.5）。

金标期望=设计意图标注（判别保守准则：误跳过检索代价＞多查一次）；语料为合成金标，
待真实用户查询语料校准后由 services/kb/business/agentic_calibration.py 复跑修订。
"""

from __future__ import annotations

import pytest

from services.kb.business.agentic import (
    _SMALLTALK_TAIL_PARTICLES,
    _SMALLTALK_TERMS,
    GRADE_SCORE_THRESHOLD,
    decide,
    grade,
)
from services.kb.business.agentic_calibration import (
    DECISION_GOLD_CASES,
    GRADE_GOLD_CASES,
    SCAN_STEP,
    DecisionCase,
    GradeCase,
    best_plateau,
    build_hits,
    grounding_gaps,
    run_decision_eval,
    scan_thresholds,
)
from services.kb.business.kb_extraction import SeedCatalog, load_seed_catalog

# 2026-09-28 PoC 标定结论（见 agentic_calibration 模块 docstring 与扫描复现用例）：
# 扫描 19 档唯一 100% 档 = 0.50（0.45/0.55 均 95.5%），维持 0.5；常量漂移须 conscious 修订。
CALIBRATED_THRESHOLD = 0.5


@pytest.fixture(scope="module")
def seed_catalog() -> SeedCatalog:
    """种子目录真装载一次（rdflib 同步解析；纯文件读取，零 PG 依赖）。"""
    return load_seed_catalog()


# ── 判别金标（38 条，三分层）──────────────────────────────────────────────────


@pytest.mark.parametrize("case", DECISION_GOLD_CASES, ids=[c.case_id for c in DECISION_GOLD_CASES])
def test_判别金标_逐条(case: DecisionCase) -> None:
    assert decide(case.query) == case.expected, f"{case.case_id} basis: {case.basis}"


def test_判别金标_规模门禁_三分层各不少于10() -> None:
    counts = {
        layer: sum(1 for c in DECISION_GOLD_CASES if c.layer == layer) for layer in ("检索必需", "无需检索", "边界灰区")
    }
    assert len(DECISION_GOLD_CASES) >= 30 and all(n >= 10 for n in counts.values()), counts


# ── 评级金标（22 条，五档+span 门禁；种子目录锚定）────────────────────────────


@pytest.mark.parametrize("case", GRADE_GOLD_CASES, ids=[c.case_id for c in GRADE_GOLD_CASES])
def test_评级金标_逐条(case: GradeCase) -> None:
    actual = grade(build_hits(case), channels=list(case.channels) if case.channels is not None else None)
    assert actual == case.expected, f"{case.case_id} basis: {case.basis}"


def test_评级金标_种子类锚定_grounding(seed_catalog: SeedCatalog) -> None:
    """每个评级场景的 seed_class 必须存在于种子目录（用种子目录构造场景的判定依据门禁）。"""
    assert grounding_gaps(seed_catalog) == []


def test_评级金标_规模门禁_五档各不少于3() -> None:
    tiers = {
        t: sum(1 for c in GRADE_GOLD_CASES if c.tier == t) for t in ("命中", "零命中", "低分", "降级单路", "含图路")
    }
    assert len(GRADE_GOLD_CASES) >= 20 and all(n >= 3 for n in tiers.values()), tiers


# ── 标定复现：金标全对 + 扫描最优档含当前常量（防常量漂移与扫描机件退化）────────


def test_标定复现_判别评级金标全对() -> None:
    decision = run_decision_eval()
    assert decision.mismatches == () and decision.accuracy == 1.0


def test_标定复现_阈值扫描最优档_当前常量在内() -> None:
    assert GRADE_SCORE_THRESHOLD == CALIBRATED_THRESHOLD  # 常量漂移须 conscious 修订本文件与扫描结论
    lo, hi, acc = best_plateau(scan_thresholds(step=SCAN_STEP))
    assert acc == 1.0, "扫描最优档不再满分——评级金标与阈值失配，须复跑标定"
    assert lo <= GRADE_SCORE_THRESHOLD <= hi, f"当前阈值 {GRADE_SCORE_THRESHOLD} 偏离最优档 [{lo}, {hi}]"


# ── 词表覆盖：寒暄词面逐词正反 + 语气尾字剥除 ────────────────────────────────


@pytest.mark.parametrize("term", sorted(_SMALLTALK_TERMS), ids=sorted(_SMALLTALK_TERMS))
def test_词面逐词_正向_整词命中跳过(term: str) -> None:
    """词面集合每一项单独成查询必须可达 skip（标定前「哈喽」被尾字剥成「哈」即由此用例抓获）。"""
    assert decide(term) == ("retrieval_skipped", "smalltalk_pattern")


@pytest.mark.parametrize(
    ("term", "particle"),
    [(term, ch) for term in sorted(_SMALLTALK_TERMS) for ch in _SMALLTALK_TAIL_PARTICLES],
    ids=[f"{term}{ch}" for term in sorted(_SMALLTALK_TERMS) for ch in _SMALLTALK_TAIL_PARTICLES],
)
def test_词面逐词_尾字变体_全积正向(term: str, particle: str) -> None:
    """词面项 × 语气尾字全积组合（词面+尾字 → 剥至词面命中）：24 词 × 14 尾字 = 336 变体。"""
    assert decide(f"{term}{particle}") == ("retrieval_skipped", "smalltalk_pattern")


@pytest.mark.parametrize("ch", _SMALLTALK_TAIL_PARTICLES, ids=list(_SMALLTALK_TAIL_PARTICLES))
def test_尾字剥除_逐字符(ch: str) -> None:
    """尾字表逐字符剥除探针（跟随后标定常量自适应）：谢谢+尾字 → 剥除后命中。"""
    assert decide(f"谢谢{ch}") == ("retrieval_skipped", "smalltalk_pattern")


@pytest.mark.parametrize(
    ("query", "expected"),
    [("在吗", ("retrieval_skipped", "smalltalk_pattern")), ("谢谢吗", ("retrieval_required", "default_retrieve"))],
)
def test_尾字负向_问号吗刻意不剥(query: str, expected: tuple[str, str]) -> None:
    """「吗」不在尾字表：在吗 core 完整保留可达词面（误剥即在→在 不可达）；谢谢吗 不被剥成
    谢谢（误剥=把非寒暄误跳检索）——保守方向放行检索。"""
    assert decide(query) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("哈哈哈", ("retrieval_required", "default_retrieve")),  # 剥至单字止且单字不命中词面
        ("你好棒", ("retrieval_required", "default_retrieve")),  # 词面超串≠词面（全词匹配口径）
        ("hello world", ("retrieval_required", "default_retrieve")),  # 空格剥除后 helloworld≠hello
        ("high", ("retrieval_required", "default_retrieve")),  # hi 的超串不误判
        ("thanksx", ("retrieval_required", "default_retrieve")),
        ("多谢帮助", ("retrieval_required", "default_retrieve")),  # 多谢的超串不误判
        ("你好呀查一下变压器", ("retrieval_required", "default_retrieve")),  # 寒暄+业务组合不判寒暄
    ],
)
def test_词面负向_形近与组合不误判(query: str, expected: tuple[str, str]) -> None:
    assert decide(query) == expected


def test_标定裁决探针_哈喽修复与弃喽变体() -> None:
    """2026-09-28 标定裁决：尾字表删「喽」——哈喽 词面项可达（修复）；谢谢喽 弃剥除保守放行检索。

    哈喽尾字即「喽」，剥除后落「哈」永不命中词面（词表不可达项）；「喽」与「哈」词面项
    冲突二选一，PoC 保词面完整（误跳过代价＞多查一次），喽尾变体失去剥除=多查一次（可接受）。
    """
    assert decide("哈喽") == ("retrieval_skipped", "smalltalk_pattern")
    assert decide("谢谢喽") == ("retrieval_required", "default_retrieve")
