"""agentic 检索阈值 PoC 标定（AgenticRAG优化方案 §7 待办①：判别/评级阈值 PoC）。

golden agentic 用例集（金标语料）+ 标定执行器。金标语料为**合成语料**（无真实用户查询
语料，结论仅在合成分布上成立——待真实语料校准后复跑）。tests/kb/test_agentic_golden.py
逐条断言金标期望（门禁防回归），本模块 main() 输出混淆矩阵+误判清单+阈值敏感性扫描，
标定结论回填 agentic.py 常量注释（只许改常量值与注释，decide/grade/rewrite 逻辑禁改）。

复跑口径（CLI，同步装载种子目录在脚本场景可接受）::

    python -m services.kb.business.agentic_calibration

- 判别金标 DECISION_GOLD_CASES：三分层 ×≥10——①明确需检索（工单号/领域实体词/检索动词）
  ②明确不需检索（寒暄/问候/道谢/纯表情/纯符号）③边界灰区（短问句/单实体词/组合句）；
  每条标注期望 (decision, decision_reason) + basis 判定依据。标注准则=设计意图（agentic.py
  判别保守性：误跳过检索的代价＞多查一次——灰区一律落检索）；
- 评级金标 GRADE_GOLD_CASES：五档 ×≥3（命中/零命中/低分/降级单路/含图路）+ span 门禁 3 条；
  hits 按 RRF 真实量级构造（rank r 单路得分 w/(k+r)，权重取 retrieve.CHANNEL_WEIGHTS[_GRAPH]
  实际常量），场景经 seed_class 锚定种子目录（load_seed_catalog 装载后校验标签存在）；
- 扫描：GRADE_SCORE_THRESHOLD 0.05~0.95 步进 0.05 逐档评测准确率+翻转清单，输出最优档。

2026-09-28 标定结论（PoC，合成金标）：判别 38/38=100%（混淆矩阵对角无泄漏）；评级扫描
19 档中 **唯一 100% 档=t=0.50**（0.45 与 0.55 均 95.5%：gl-1 归一 0.45、gd-4 归一 0.5 恰在
边界）→ GRADE_SCORE_THRESHOLD=0.5 为实测最优档，维持不变；词面唯一误判「哈喽」
归因语气尾字「喽」剥除冲突（词表不可达项）→ 尾字表删「喽」（代价：谢谢喽等喽尾变体保守
放行检索），词面集合零增删。详见各常量注释与 tests/kb/test_agentic_golden.py。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from services.kb.business import agentic
from services.kb.business.kb_extraction import SeedCatalog, load_seed_catalog
from services.kb.retrieval.retrieve import RRF_K, SearchHit

CALIBRATION_VERSION = "agentic-golden-poc@v1"  # 金标版本（复跑对齐用）
CALIBRATION_DATE = "2026-09-28"  # 标定日期（常量注释引用同源）
SCAN_LOW, SCAN_HIGH, SCAN_STEP = 0.05, 0.95, 0.05  # 阈值扫描网格（任务口径）

# 两/三路基线理论满分（retrieve.CHANNEL_WEIGHTS[_GRAPH] 同源推导，仅注释可读性用）
_TWO_MAX = 1.0 / (RRF_K + 1)  # (0.5+0.5)/61
_ONE_MAX = 0.5 / (RRF_K + 1)  # 单路降级 0.5/61
_GRAPH_MAX = 1.4 / (RRF_K + 1)  # (0.4+0.4+0.6)/61


# ---------------------------------------------------------------- 金标语料：判别（38 条）


@dataclass(frozen=True, slots=True)
class DecisionCase:
    """判别金标行：query → 期望 (decision, decision_reason) + basis 判定依据。"""

    case_id: str
    layer: str  # 检索必需 | 无需检索 | 边界灰区
    kind: str  # 子类（工单号/实体词/检索动词/寒暄/道谢/纯表情/纯符号/短问句/单实体词/组合句/应答词）
    query: str
    expected: tuple[str, str]
    basis: str  # 判定依据（金标标注理由，须可回指规则或设计准则）


DECISION_GOLD_CASES: tuple[DecisionCase, ...] = (
    # ── ① 明确需检索（12）：电力域业务问句，含确定性工单号/领域实体词/检索动词 ──
    DecisionCase(
        "dr-01",
        "检索必需",
        "工单号",
        "帮我查下OO-654321的复电时间",
        ("retrieval_required", "deterministic_task"),
        "OO-\\d{6} 命中（R004 SHACL pattern 同款）→ 确定性任务免判别直查",
    ),
    DecisionCase(
        "dr-02",
        "检索必需",
        "工单号",
        "GD-20240101-0001的停电原因是什么",
        ("retrieval_required", "deterministic_task"),
        "GD-\\d{8}-\\d{4} 故障工单模式命中 → deterministic_task",
    ),
    DecisionCase(
        "dr-03",
        "检索必需",
        "工单号",
        "查询GD-20250927-0012的抢修班组到场情况",
        ("retrieval_required", "deterministic_task"),
        "GD- 工单号子串命中（regex search 不要求全串）",
    ),
    DecisionCase(
        "dr-04",
        "检索必需",
        "工单号",
        "OO-123456",
        ("retrieval_required", "deterministic_task"),
        "裸工单号直查（最小确定性任务形态）",
    ),
    DecisionCase(
        "dr-05",
        "检索必需",
        "工单号",
        "GD-20240101-0001",
        ("retrieval_required", "deterministic_task"),
        "裸故障工单号直查",
    ),
    DecisionCase(
        "dr-06",
        "检索必需",
        "实体词",
        "变压器油温超标怎么处理",
        ("retrieval_required", "default_retrieve"),
        "领域实体词（变压器=种子类标签）+ 疑问 → 默认检索",
    ),
    DecisionCase(
        "dr-07",
        "检索必需",
        "实体词",
        "馈线F001故障隔离流程",
        ("retrieval_required", "default_retrieve"),
        "实体词+编号（馈线=种子类标签）；无工单模式 → 默认检索",
    ),
    DecisionCase(
        "dr-08",
        "检索必需",
        "实体词",
        "配电线路巡检周期是多久",
        ("retrieval_required", "default_retrieve"),
        "配电线路=种子类标签，业务问句 → 默认检索",
    ),
    DecisionCase(
        "dr-09",
        "检索必需",
        "实体词",
        "复电事件一般由谁确认",
        ("retrieval_required", "default_retrieve"),
        "复电事件=种子类标签；非寒暄非工单 → 默认检索",
    ),
    DecisionCase(
        "dr-10",
        "检索必需",
        "检索动词",
        "帮我检索停电分析报告模板",
        ("retrieval_required", "default_retrieve"),
        "显式检索动词「检索」+ 停电分析报告=种子类 → 默认检索",
    ),
    DecisionCase(
        "dr-11",
        "检索必需",
        "检索动词",
        "计量表抄表异常如何申报",
        ("retrieval_required", "default_retrieve"),
        "计量表=种子类标签业务问句；无寒暄词面 → 默认检索",
    ),
    DecisionCase(
        "dr-12",
        "检索必需",
        "检索动词",
        "检索变压器与计量表互斥规则",
        ("retrieval_required", "default_retrieve"),
        "检索动词+种子类（R001 业务规则查询）→ 默认检索",
    ),
    # ── ② 明确不需检索（14）：寒暄/问候/道谢/纯表情/纯符号 ──
    DecisionCase(
        "nr-01",
        "无需检索",
        "寒暄",
        "你好",
        ("retrieval_skipped", "smalltalk_pattern"),
        "词面集合命中（§5 场景 1 零召回成本直答）",
    ),
    DecisionCase(
        "nr-02",
        "无需检索",
        "寒暄",
        "您好",
        ("retrieval_skipped", "smalltalk_pattern"),
        "词面集合命中（尾字「好」非语气字，core 不剥）",
    ),
    DecisionCase(
        "nr-03",
        "无需检索",
        "寒暄",
        "大家好",
        ("retrieval_skipped", "smalltalk_pattern"),
        "词面集合命中",
    ),
    DecisionCase(
        "nr-04",
        "无需检索",
        "寒暄",
        "哈喽",
        ("retrieval_skipped", "smalltalk_pattern"),
        "词面集合命中——2026-09-28 标定修复项：尾字表删「喽」后 core=哈喽 可达词面（标定前剥喽落「哈」误放行检索）",
    ),
    DecisionCase(
        "nr-05",
        "无需检索",
        "寒暄",
        "哈罗",
        ("retrieval_skipped", "smalltalk_pattern"),
        "词面集合命中（罗非语气尾字）",
    ),
    DecisionCase(
        "nr-06",
        "无需检索",
        "寒暄",
        "hello",
        ("retrieval_skipped", "smalltalk_pattern"),
        "词面集合命中（大小写不敏感归一）",
    ),
    DecisionCase(
        "nr-07",
        "无需检索",
        "寒暄",
        "THANK YOU",
        ("retrieval_skipped", "smalltalk_pattern"),
        "剥空白+小写 → thankyou 命中词面（空格剥离口径探针）",
    ),
    DecisionCase(
        "nr-08",
        "无需检索",
        "道谢",
        "谢谢！！！",
        ("retrieval_skipped", "smalltalk_pattern"),
        "标点剥除 → 谢谢 命中",
    ),
    DecisionCase(
        "nr-09",
        "无需检索",
        "纯表情",
        "😀😀",
        ("retrieval_skipped", "smalltalk_pattern"),
        "纯表情：\\W 剥除后 core=空 → skip",
    ),
    DecisionCase(
        "nr-10",
        "无需检索",
        "纯符号",
        "？？？",
        ("retrieval_skipped", "smalltalk_pattern"),
        "纯标点：core=空 → skip（问号无查询语义）",
    ),
    DecisionCase(
        "nr-11",
        "无需检索",
        "纯符号",
        "~~~",
        ("retrieval_skipped", "smalltalk_pattern"),
        "纯符号：core=空 → skip",
    ),
    DecisionCase(
        "nr-12",
        "无需检索",
        "问候",
        "早上好呀",
        ("retrieval_skipped", "smalltalk_pattern"),
        "尾字「呀」剥除 → 早上好 命中",
    ),
    DecisionCase(
        "nr-13",
        "无需检索",
        "问候",
        "在吗",
        ("retrieval_skipped", "smalltalk_pattern"),
        "词面集合命中（「吗」刻意不在尾字表——见 nr-14 反例与尾字探针）",
    ),
    DecisionCase(
        "nr-14",
        "无需检索",
        "道谢",
        "再见啦",
        ("retrieval_skipped", "smalltalk_pattern"),
        "尾字「啦」剥除 → 再见 命中",
    ),
    # ── ③ 边界灰区（12）：短问句/单实体词/组合句寒暄+业务——标注准则=保守放行检索 ──
    DecisionCase(
        "gz-01",
        "边界灰区",
        "单实体词",
        "停电",
        ("retrieval_required", "default_retrieve"),
        "单实体泛词无寒暄词面；保守准则（误跳过代价＞多查一次）→ 检索",
    ),
    DecisionCase(
        "gz-02",
        "边界灰区",
        "单实体词",
        "变压器",
        ("retrieval_required", "default_retrieve"),
        "单实体词=种子类标签；用户可能想查类目资料 → 保守检索",
    ),
    DecisionCase(
        "gz-03",
        "边界灰区",
        "短问句",
        "停电了吗",
        ("retrieval_required", "default_retrieve"),
        "短问句含业务实体；「吗」不剥（非语气尾字）→ 非寒暄 → 检索",
    ),
    DecisionCase(
        "gz-04",
        "边界灰区",
        "短问句",
        "怎么复电",
        ("retrieval_required", "default_retrieve"),
        "短问句含业务动词 → 检索",
    ),
    DecisionCase(
        "gz-05",
        "边界灰区",
        "组合句",
        "你好，查一下OO-123456",
        ("retrieval_required", "deterministic_task"),
        "组合句不判寒暄（core 含业务段）→ 工单号模式优先 deterministic_task",
    ),
    DecisionCase(
        "gz-06",
        "边界灰区",
        "组合句",
        "你好，变压器油温多少算正常",
        ("retrieval_required", "default_retrieve"),
        "寒暄+业务组合：core 非词面命中 → 默认检索",
    ),
    DecisionCase(
        "gz-07",
        "边界灰区",
        "组合句",
        "谢谢，再看下GD-20240102-0003的进展",
        ("retrieval_required", "deterministic_task"),
        "道谢开头不吞业务段：GD- 工单号子串命中 → 直查",
    ),
    DecisionCase(
        "gz-08",
        "边界灰区",
        "组合句",
        "oo-123456",
        ("retrieval_required", "default_retrieve"),
        "小写工单号：模式大小写敏感（与 SHACL R004 一致）→ 不判确定性，保守默认检索",
    ),
    DecisionCase(
        "gz-09",
        "边界灰区",
        "短问句",
        "GD-20240101-00",
        ("retrieval_required", "default_retrieve"),
        "近失模式（尾段 2 位≠\\d{4}）不误判工单 → 默认检索",
    ),
    DecisionCase(
        "gz-10",
        "边界灰区",
        "应答词",
        "嗯嗯",
        ("retrieval_required", "default_retrieve"),
        "纯应答词不在词面集合；PoC 裁决保守不扩表（跳过收益低、扩表增误跳风险）→ 检索（模拟口径，待真实语料复核）",
    ),
    DecisionCase(
        "gz-11",
        "边界灰区",
        "应答词",
        "哈哈哈",
        ("retrieval_required", "default_retrieve"),
        "笑声词面外（「哈」在尾字表但剥至单字止、单字不命中词面）→ 检索（既有用例同口径）",
    ),
    DecisionCase(
        "gz-12",
        "边界灰区",
        "组合句",
        "hi，馈线F001最近有停电吗",
        ("retrieval_required", "default_retrieve"),
        "英文寒暄+业务组合：core 含业务段非词面命中；无工单模式 → 默认检索",
    ),
)


# ---------------------------------------------------------------- 金标语料：评级（22 条）


@dataclass(frozen=True, slots=True)
class HitSpec:
    """命中构造规格：score=RRF 融合分（w/(k+r) 口径），span=出处指针，channels=命中路。"""

    score: float
    span: tuple[int, int] | None
    channels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GradeCase:
    """评级金标行：hits 场景（种子类锚定）→ 期望 (grade, grade_reason) + basis。"""

    case_id: str
    tier: str  # 命中 | 零命中 | 低分 | 降级单路 | 含图路 | span门禁
    scenario: str
    seed_class: str  # 场景锚定的种子类标签（load_seed_catalog 装载后校验存在）
    hits: tuple[HitSpec, ...]
    channels: tuple[str, ...] | None  # 显式通道集（None=由 hits 自带 channels 推断）
    expected: tuple[str, str]
    basis: str  # 判定依据（归一分数算式）


def _spec(score: float, span: tuple[int, int] | None = (0, 4), channels: tuple[str, ...] = ()) -> HitSpec:
    return HitSpec(score=score, span=span, channels=channels)


GRADE_GOLD_CASES: tuple[GradeCase, ...] = (
    # ── 零命中（3）───────────────────────────────────────────────────────────
    GradeCase(
        "gz-hit-1",
        "零命中",
        "配变停电影响查询零召回",
        "变压器",
        (),
        None,
        ("fail", "hit_count_zero"),
        "hits 空 → 首判 hit_count_zero（先于分数/span）",
    ),
    GradeCase(
        "gz-hit-2",
        "零命中",
        "工单号直查零召回",
        "停电工单",
        (),
        None,
        ("fail", "hit_count_zero"),
        "hits 空（工单号仅免判别，不保证命中）",
    ),
    GradeCase(
        "gz-hit-3",
        "零命中",
        "无关词零召回",
        "客户",
        (),
        None,
        ("fail", "hit_count_zero"),
        "hits 空 → 触发纠错轮前置条件",
    ),
    # ── 命中（4）────────────────────────────────────────────────────────────
    GradeCase(
        "gp-1",
        "命中",
        "两路双通道 rank1（生产可达量级 1/61）",
        "馈线",
        (_spec(1.0 / (RRF_K + 1), channels=("bm25", "vector")),),
        ("bm25", "vector"),
        ("pass", "pass"),
        f"归一={(1.0 / (RRF_K + 1)):.6f}/{_TWO_MAX:.6f}=1.0 ≥ 阈值",
    ),
    GradeCase(
        "gp-2",
        "命中",
        "两路双通道 rank2",
        "馈线",
        (_spec(0.5 / (RRF_K + 2) + 0.5 / (RRF_K + 2), channels=("bm25", "vector")),),
        ("bm25", "vector"),
        ("pass", "pass"),
        "归一=61/62≈0.984 ≥ 阈值",
    ),
    GradeCase(
        "gp-3",
        "命中",
        "多命中 top1 带 span（次位 span 缺不连坐）",
        "变压器",
        (
            _spec(1.0 / (RRF_K + 1), channels=("bm25", "vector")),
            _spec(1.0 / (RRF_K + 2), span=None, channels=("bm25", "vector")),
        ),
        ("bm25", "vector"),
        ("pass", "pass"),
        "top1 有 span 即可（span 门禁只看全缺）",
    ),
    GradeCase(
        "gp-4",
        "命中",
        "召回池底弱命中（rank50 双路）",
        "配电线路",
        (_spec(0.5 / (RRF_K + 50) + 0.5 / (RRF_K + 50), channels=("bm25", "vector")),),
        ("bm25", "vector"),
        ("pass", "pass"),
        "归一=61/110≈0.554≥0.5：RECALL_POOL=50 内的最弱可达命中不应被弃（阈值上界约束 0.55）",
    ),
    # ── 低分（4）────────────────────────────────────────────────────────────
    GradeCase(
        "gl-1",
        "低分",
        "两路归一 0.45（阈值下界探针）",
        "变压器",
        (_spec(0.45 / (RRF_K + 1), channels=("bm25", "vector")),),
        ("bm25", "vector"),
        ("fail", "score_below_threshold"),
        "归一=0.45<0.5：恰在最优档下界之下（阈值若降至 0.45 即翻转 → 扫描下界证据）",
    ),
    GradeCase(
        "gl-2",
        "低分",
        "两路归一 0.10 极低",
        "计量表",
        (_spec(0.10 / (RRF_K + 1), channels=("bm25", "vector")),),
        ("bm25", "vector"),
        ("fail", "score_below_threshold"),
        "归一=0.10：远低阈值（如 rank 600 长尾）",
    ),
    GradeCase(
        "gl-3",
        "低分",
        "图路唯一命中 rank5（弱图召回）",
        "保护装置",
        (_spec(0.6 / (RRF_K + 5), channels=("bm25", "vector", "graph")),),
        ("bm25", "vector", "graph"),
        ("fail", "score_below_threshold"),
        "归一=(0.6/65)/(1.4/61)≈0.402<0.5：图路单独低排不保过（阈值下界约束 0.45）",
    ),
    GradeCase(
        "gl-4",
        "低分",
        "降级单路归一 0.10",
        "变电站",
        (_spec(0.05 / (RRF_K + 1), channels=("bm25",)),),
        ("bm25",),
        ("fail", "score_below_threshold"),
        "归一=(0.05/61)/(0.5/61)=0.10：单路降级同口径拦截",
    ),
    # ── 降级单路（4）────────────────────────────────────────────────────────
    GradeCase(
        "gd-1",
        "降级单路",
        "BM25-only rank1（vector_unavailable 降级形态）",
        "馈线",
        (_spec(0.5 / (RRF_K + 1), channels=("bm25",)),),
        ("bm25",),
        ("pass", "pass"),
        f"归一={(0.5 / (RRF_K + 1)):.6f}/{_ONE_MAX:.6f}=1.0：降级不因换算基准漂移恒挂",
    ),
    GradeCase(
        "gd-2",
        "降级单路",
        "BM25-only rank3",
        "馈线",
        (_spec(0.5 / (RRF_K + 3), channels=("bm25",)),),
        ("bm25",),
        ("pass", "pass"),
        "归一=61/63≈0.968",
    ),
    GradeCase(
        "gd-3",
        "降级单路",
        "BM25-only rank20 中段",
        "停电工单",
        (_spec(0.5 / (RRF_K + 20), channels=("bm25",)),),
        ("bm25",),
        ("pass", "pass"),
        "归一=61/80≈0.763",
    ),
    GradeCase(
        "gd-4",
        "降级单路",
        "显式两路通道参数+单路命中",
        "停电事件",
        (_spec(0.5 / (RRF_K + 1), channels=("bm25",)),),
        ("bm25", "vector"),
        ("pass", "pass"),
        "调用方已知两路参与：归一=(0.5/61)/(1.0/61)=0.5 恰等于阈值，严格小于不判低分"
        " → 边界保过（既有 F1 显式通道参数用例同口径）",
    ),
    # ── 含图路（4）──────────────────────────────────────────────────────────
    GradeCase(
        "gw-1",
        "含图路",
        "三路全通道 rank1（图路生效满分形态）",
        "变压器",
        (_spec(1.4 / (RRF_K + 1), channels=("bm25", "vector", "graph")),),
        ("bm25", "vector", "graph"),
        ("pass", "pass"),
        f"归一={(1.4 / (RRF_K + 1)):.6f}/{_GRAPH_MAX:.6f}=1.0（三路权重表 Σ=1.4）",
    ),
    GradeCase(
        "gw-2",
        "含图路",
        "图路 rank1 + BM25 rank2",
        "线路区段",
        (_spec(0.6 / (RRF_K + 1) + 0.4 / (RRF_K + 2), channels=("bm25", "vector", "graph")),),
        ("bm25", "vector", "graph"),
        ("pass", "pass"),
        "归一≈0.710：图权 0.6 主导仍过阈",
    ),
    GradeCase(
        "gw-3",
        "含图路",
        "图路 rank3 + 双路 rank1",
        "抢修班组",
        (_spec(0.4 / (RRF_K + 1) + 0.4 / (RRF_K + 1) + 0.6 / (RRF_K + 3), channels=("bm25", "vector", "graph")),),
        ("bm25", "vector", "graph"),
        ("pass", "pass"),
        "归一≈0.986：多路共鸣",
    ),
    GradeCase(
        "gw-4",
        "含图路",
        "图路唯一命中 rank40 长尾",
        "复电事件",
        (_spec(0.6 / (RRF_K + 40), channels=("bm25", "vector", "graph")),),
        ("bm25", "vector", "graph"),
        ("fail", "score_below_threshold"),
        "归一=(0.6/100)/(1.4/61)≈0.261：长尾图扩展不保过",
    ),
    # ── span 门禁（3，与阈值无关的出处指针纪律）────────────────────────────
    GradeCase(
        "gs-1",
        "span门禁",
        "两路满分但 span 全缺",
        "停电事件",
        (_spec(1.0 / (RRF_K + 1), span=None, channels=("bm25", "vector")),),
        ("bm25", "vector"),
        ("fail", "span_missing"),
        "引用无法回指原文位置（§5.2 出处指针）→ 分数再高也 fail",
    ),
    GradeCase(
        "gs-2",
        "span门禁",
        "多命中全部缺 span",
        "馈线",
        (
            _spec(1.0 / (RRF_K + 1), span=None, channels=("bm25", "vector")),
            _spec(1.0 / (RRF_K + 2), span=None, channels=("bm25", "vector")),
        ),
        ("bm25", "vector"),
        ("fail", "span_missing"),
        "all(span is None) → fail（任一 top1 有 span 即免）",
    ),
    GradeCase(
        "gs-3",
        "span门禁",
        "含图路满分缺 span",
        "停电确认事件",
        (_spec(1.4 / (RRF_K + 1), span=None, channels=("bm25", "vector", "graph")),),
        ("bm25", "vector", "graph"),
        ("fail", "span_missing"),
        "三路同口径：span 门禁先于 pass 且与通道集无关",
    ),
)


def build_hits(case: GradeCase) -> list[SearchHit]:
    """金标行 → SearchHit 列表（uuid 现生成；span/channels 按 HitSpec 回填）。"""
    return [
        SearchHit(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            content=f"{case.seed_class}场景命中片段",
            score=spec.score,
            span=list(spec.span) if spec.span is not None else None,
            channels=list(spec.channels),
        )
        for spec in case.hits
    ]


# ---------------------------------------------------------------- 标定执行器


@dataclass(frozen=True, slots=True)
class DecisionReport:
    """判别金标评测报告：混淆矩阵（期望×实际，按「是否需检索」二值折叠）+ 逐项误判。"""

    total: int
    correct: int
    matrix: dict[tuple[str, str], int]  # (期望二值, 实际二值) → 计数；二值 ∈ required/skipped
    mismatches: tuple[tuple[str, str, tuple[str, str], tuple[str, str]], ...]  # (id, query, 期望, 实际)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class GradeReport:
    """评级金标评测报告：总分档准确率 + 分档（tier）细分 + 逐项误判。"""

    threshold: float
    total: int
    correct: int
    by_tier: dict[str, tuple[int, int]]  # tier → (正确, 总数)
    mismatches: tuple[tuple[str, str, tuple[str, str], tuple[str, str]], ...]

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def _binary(decision: str) -> str:
    return "skipped" if decision == "retrieval_skipped" else "required"


def run_decision_eval(cases: Sequence[DecisionCase] = DECISION_GOLD_CASES) -> DecisionReport:
    """跑判别金标：decide() 实测 vs 期望，产出混淆矩阵与误判清单。"""
    matrix: dict[tuple[str, str], int] = {}
    mismatches: list[tuple[str, str, tuple[str, str], tuple[str, str]]] = []
    correct = 0
    for case in cases:
        actual = agentic.decide(case.query)
        key = (_binary(case.expected[0]), _binary(actual[0]))
        matrix[key] = matrix.get(key, 0) + 1
        if actual == case.expected:
            correct += 1
        else:
            mismatches.append((case.case_id, case.query, case.expected, actual))
    return DecisionReport(total=len(cases), correct=correct, matrix=matrix, mismatches=tuple(mismatches))


def run_grade_eval(cases: Sequence[GradeCase] = GRADE_GOLD_CASES, *, threshold: float | None = None) -> GradeReport:
    """跑评级金标：grade() 实测 vs 期望。threshold=None 用当前常量；否则临时替换模块常量扫描。"""
    token = agentic.GRADE_SCORE_THRESHOLD
    if threshold is not None:
        agentic.GRADE_SCORE_THRESHOLD = threshold
    try:
        by_tier: dict[str, tuple[int, int]] = {}
        mismatches: list[tuple[str, str, tuple[str, str], tuple[str, str]]] = []
        correct = 0
        for case in cases:
            explicit = list(case.channels) if case.channels is not None else None
            actual = agentic.grade(build_hits(case), channels=explicit)
            ok = actual == case.expected
            correct += ok
            prev = by_tier.get(case.tier, (0, 0))
            by_tier[case.tier] = (prev[0] + ok, prev[1] + 1)
            if not ok:
                mismatches.append((case.case_id, case.scenario, case.expected, actual))
        return GradeReport(
            threshold=agentic.GRADE_SCORE_THRESHOLD,
            total=len(cases),
            correct=correct,
            by_tier=by_tier,
            mismatches=tuple(mismatches),
        )
    finally:
        agentic.GRADE_SCORE_THRESHOLD = token  # 扫描后还原：调用方后续 grade 行为不受污染


@dataclass(frozen=True, slots=True)
class ScanPoint:
    """单档扫描点：阈值 → 准确率 + 该档误判用例 id（相对金标期望的翻转清单）。"""

    threshold: float
    accuracy: float
    wrong_ids: tuple[str, ...]


def scan_thresholds(
    cases: Sequence[GradeCase] = GRADE_GOLD_CASES,
    *,
    low: float = SCAN_LOW,
    high: float = SCAN_HIGH,
    step: float = SCAN_STEP,
) -> tuple[ScanPoint, ...]:
    """阈值敏感性扫描：0.05~0.95 步进 0.05 逐档评测，输出准确率曲线与翻转清单。"""
    points: list[ScanPoint] = []
    steps = round((high - low) / step)
    for i in range(steps + 1):
        t = round(low + i * step, 2)
        report = run_grade_eval(cases, threshold=t)
        points.append(
            ScanPoint(threshold=t, accuracy=report.accuracy, wrong_ids=tuple(m[0] for m in report.mismatches))
        )
    return tuple(points)


def best_plateau(points: Sequence[ScanPoint]) -> tuple[float, float, float]:
    """准确率曲线最优档（连续网格区间）：返回 (下界, 上界, 最优准确率)；并列取首个区间。"""
    best = max(p.accuracy for p in points)
    edges = [p.threshold for p in points if p.accuracy == best]
    return edges[0], edges[-1], best


def grounding_gaps(catalog: SeedCatalog) -> list[str]:
    """评级金标场景的种子类锚定校验：返回缺失标签清单（空=全部锚定成立）。"""
    labels = {label for _iri, label, _local in catalog.classes}
    return sorted({case.seed_class for case in GRADE_GOLD_CASES} - labels)


def main() -> None:  # pragma: no cover - CLI 报告入口（tests 复算扫描，不经此）
    """跑金标集输出标定结论文本（混淆矩阵+误判清单+阈值扫描曲线）。"""
    catalog = load_seed_catalog()  # CLI 脚本场景同步装载可接受（生产请求路径才要求 to_thread）
    gaps = grounding_gaps(catalog)
    print(f"== agentic 阈值 PoC 标定 {CALIBRATION_VERSION}（{CALIBRATION_DATE}，合成金标·待真实语料校准）==")

    decision = run_decision_eval()
    print(
        f"\n[判别金标] {decision.total} 条（必需={sum(1 for c in DECISION_GOLD_CASES if c.layer == '检索必需')}"
        f"/无需={sum(1 for c in DECISION_GOLD_CASES if c.layer == '无需检索')}"
        f"/灰区={sum(1 for c in DECISION_GOLD_CASES if c.layer == '边界灰区')}）"
        f" 准确率 {decision.accuracy:.1%}（{decision.correct}/{decision.total}）"
    )
    print(f"混淆矩阵（期望×实际）: {dict(sorted(decision.matrix.items()))}")
    for case_id, query, expected, actual in decision.mismatches:
        print(f"  误判 {case_id} {query!r}: 期望{expected} 实际{actual}")
    if not decision.mismatches:
        print("  误判清单: 空")

    grade = run_grade_eval()
    tiers = " ".join(f"{tier}={ok}/{total}" for tier, (ok, total) in sorted(grade.by_tier.items()))
    print(
        f"\n[评级金标] {grade.total} 条 阈值={grade.threshold} 准确率 {grade.accuracy:.1%}"
        f"（{grade.correct}/{grade.total}） 分档: {tiers}"
    )
    for case_id, scenario, expected, actual in grade.mismatches:
        print(f"  误判 {case_id} {scenario}: 期望{expected} 实际{actual}")
    if not grade.mismatches:
        print("  误判清单: 空")

    points = scan_thresholds()
    lo, hi, acc = best_plateau(points)
    print("\n[阈值扫描] GRADE_SCORE_THRESHOLD 0.05~0.95 步进 0.05：")
    for point in points:
        mark = " ←最优档" if point.accuracy == acc else ""
        print(f"  t={point.threshold:.2f} acc={point.accuracy:.1%} flipped={list(point.wrong_ids)}{mark}")
    print(f"最优档: [{lo:.2f}, {hi:.2f}]（acc={acc:.1%}） 当前常量={agentic.GRADE_SCORE_THRESHOLD}")

    print(f"\n[种子类锚定] {'通过（全部标签存在）' if not gaps else f'缺失: {gaps}'}")
    inside = "位于最优档内" if lo <= agentic.GRADE_SCORE_THRESHOLD <= hi else "偏离最优档"
    print(
        f"\n[结论] 判别 {decision.accuracy:.0%} / 评级@当前阈值 {grade.accuracy:.0%}；"
        f"最优档 [{lo:.2f}, {hi:.2f}]，当前阈值{inside}。"
        "合成语料结论——待真实用户查询语料校准后复跑。"
    )


if __name__ == "__main__":
    main()
