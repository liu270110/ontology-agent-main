"""A0 服务端代跑 agentic 检索内核 v1（纯规则档，LLM 档关闭）。

设计依据：docs/OntRAG/AgenticRAG优化方案.md §3（服务端 agentic 检索内核：判别→检索→
评级→纠错的每步留 trace）与 §8.1（冻结契约：AgenticTrace 字段/枚举逐字段一致）。

v1 范围（方案 §4 分期表 v1 行 + §7 待办「阈值未标定前 LLM 档默认关闭=纯规则档」）：
- M1 判别（decide）：纯规则词面——寒暄/纯表情 → 跳过检索；工单号模式（OO- 与种子本体
  R004 SHACL pattern 同款 / GD-）→ 确定性任务免判别直查；其余 → 默认检索。
- M3 评级（grade）：规则先行——命中数 0 / top1 分低于阈值 / 引用 span 全缺 → 判失败。
- M3 纠错（rewrite + run_agentic_search）：术语归一受限改写（复用 kb_extraction 的
  种子目录与 match_seed_class 一级对齐，只读 import 不改该文件）→ 改写重查
  （≤max_rounds 轮，改写依据进 trace）→ 仍失败 → degraded="agentic_exhausted"
  （§5 场景 4：degraded 理由回传，不编造答案）。

宪法对齐：推理分级（宪法 2）——本档所有决策点均为确定性规则，零 LLM 调用；trace 全链
（宪法 5）——每轮 action/query/rewrite_basis/grade/grade_reason 进 AgenticTrace.rounds，
explain_trace_id 贯穿（§2.3 knowledge.explain 的 v1 数据源）。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.kb.business.kb_extraction import SeedCatalog, load_seed_catalog, match_seed_class
from services.kb.retrieval.retrieve import SearchHit

# ---------------------------------------------------------------- §8.1 冻结契约类型

AgenticMode = Literal["rule"]  # v1 固定 "rule"（LLM 档关闭，阈值 PoC 后开 "hybrid"）
AgenticDecision = Literal["retrieval_required", "retrieval_skipped"]
AgenticDecisionReason = Literal["deterministic_task", "smalltalk_pattern", "default_retrieve"]
AgenticAction = Literal["search", "rewrite_search"]
AgenticGrade = Literal["pass", "fail"]
AgenticGradeReason = Literal["pass", "hit_count_zero", "score_below_threshold", "span_missing"]
AgenticDegraded = Literal["agentic_exhausted"]

EXPLAIN_TRACE_PREFIX = "kb-agentic:"  # §8.1：explain_trace_id = 前缀 + uuid（knowledge.explain 回放键）

# 评级阈值（§2.2 G2 规则先行评级）：top1 分低于此值判 score_below_threshold。
# 0.15 为占位示例值——待 PoC 标定（golden QA 扩展 agentic 用例集后冻结，方案 §7 待办）。
GRADE_SCORE_THRESHOLD = 0.15


class AgenticRound(BaseModel):
    """单轮时间线条目（§8.1 rounds 元素契约：前端 AgenticTracePanel 步进数据源）。"""

    model_config = ConfigDict(frozen=True)

    seq: int  # 1 起步轮号
    action: AgenticAction  # search | rewrite_search
    query: str  # 本轮实际检索词（原始查询或改写后查询）
    rewrite_basis: str | None = None  # 改写依据（term_alias:<标签>）；search 轮恒 None
    grade: AgenticGrade
    grade_reason: AgenticGradeReason


class AgenticTrace(BaseModel):
    """agentic 检索循环 trace（§8.1 冻结契约，前后端对接面；值对象 frozen）。"""

    model_config = ConfigDict(frozen=True)

    mode: AgenticMode = "rule"
    decision: AgenticDecision
    decision_reason: AgenticDecisionReason
    rounds: list[AgenticRound] = Field(default_factory=list)  # 0~max_rounds 轮（skip 时为空）
    degraded: AgenticDegraded | None = None  # "agentic_exhausted"=纠错轮用尽仍失败（前端显著提示）
    explain_trace_id: str = Field(default_factory=lambda: f"{EXPLAIN_TRACE_PREFIX}{uuid.uuid4()}")


# ---------------------------------------------------------------- M1 判别（纯规则）

# 寒暄词面小集合（§3 ①：规则词面判别先行）。匹配口径=整条查询剥除空白/标点/表情符号、
# 去语气尾字后全词命中（大小写不敏感）——组合句（如「你好，查一下OO-…」）不判寒暄，落回
# 工单号/默认分支。小集合刻意保守：误跳过检索的代价（漏召回）远大于多查一次。
_SMALLTALK_TERMS = frozenset(
    {
        "你好",
        "您好",
        "大家好",
        "哈喽",
        "哈罗",
        "嗨",
        "hi",
        "hello",
        "hey",
        "谢谢",
        "多谢",
        "感谢",
        "thanks",
        "thankyou",
        "再见",
        "拜拜",
        "bye",
        "晚安",
        "早安",
        "早上好",
        "下午好",
        "晚上好",
        "在吗",
        "在么",
    }
)
# 语气尾字（剥除后允许「谢谢呀/你好啊」等口语变体；逐字剥除至不再命中）
_SMALLTALK_TAIL_PARTICLES = "呀啊哈呢哦噢喔呗啦咯喽哟捏嘛吧您"

# 工单号模式（确定性任务判别，§2.2 G1「任务本体先判」的 v1 词面版）：
# OO-\d{6} 与种子本体 R004 SHACL pattern ^OO-[0-9]{6}$ 同款（seeds/power_seed.ttl）；
# GD-\d{8}-\d{4} = 故障工单（停电分析 wedge 场景约定）。大小写敏感（与 SHACL pattern 一致）。
_TICKET_NO_RE = re.compile(r"GD-\d{8}-\d{4}|OO-\d{6}")

_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)  # 空白/标点/符号/表情（\w 含 CJK，不含 emoji）


def _politesse_core(query: str) -> str:
    """寒暄匹配核心形：剥空白/标点/符号/表情 → 去语气尾字 → 小写（与词面集合同形归一）。"""
    core = _NON_WORD_RE.sub("", query).lower()
    while len(core) > 1 and core[-1] in _SMALLTALK_TAIL_PARTICLES:
        core = core[:-1]
    return core


def decide(query: str) -> tuple[AgenticDecision, AgenticDecisionReason]:
    """要不要检索（M1 判别，纯规则词面版；§3 ①）。

    - 寒暄/纯表情（整条查询为词面小集合命中或剥除后为空）→ retrieval_skipped/smalltalk_pattern
      （§5 场景 1：零召回成本直答）；
    - 工单号模式（``GD-\\d{8}-\\d{4}`` / ``OO-\\d{6}``）→ retrieval_required/deterministic_task
      （确定性任务免判别直查；大小写敏感与 SHACL pattern 一致）；
    - 其余 → retrieval_required/default_retrieve。
    """
    core = _politesse_core(query)
    if core == "" or core in _SMALLTALK_TERMS:
        return "retrieval_skipped", "smalltalk_pattern"
    if _TICKET_NO_RE.search(query):
        return "retrieval_required", "deterministic_task"
    return "retrieval_required", "default_retrieve"


# ---------------------------------------------------------------- M3 评级（纯规则）


def grade(hits: Sequence[SearchHit]) -> tuple[AgenticGrade, AgenticGradeReason]:
    """查得好不好（M3 评级，规则先行版；§3 ④。hits 须为融合降序——hybrid_search 输出序）。

    - 命中数 0 → fail/hit_count_zero；
    - top1 score < GRADE_SCORE_THRESHOLD（0.15，待 PoC 标定）→ fail/score_below_threshold；
    - 有命中但 span 全缺（引用无法回指原文位置，出处指针纪律 §5.2）→ fail/span_missing；
    - 否则 pass/pass。
    """
    if not hits:
        return "fail", "hit_count_zero"
    if hits[0].score < GRADE_SCORE_THRESHOLD:
        return "fail", "score_below_threshold"
    if all(hit.span is None for hit in hits):
        return "fail", "span_missing"
    return "pass", "pass"


# ---------------------------------------------------------------- M3 纠错（术语归一受限改写）

# 改写否定词表（v1 示例值）：常见领域泛词是类标签的子串（如「停电」⊂「停电工单」），按包含
# 归一会把泛词错升为类名。术语表（主文档 §4.3#3 aliases）落地前先以小词表挡住高频误归一；
# 待术语表正式承载别名后由 aliases 取代本词表。
_REWRITE_BLOCKLIST = frozenset({"停电", "复电", "故障", "抢修", "工单", "报告", "事件", "任务"})

_TERM_RE = re.compile(r"\w+", re.UNICODE)  # CJK/字母/数字连续段（\w 含 CJK；含 emoji 无影响）

_DEFAULT_CATALOG_CACHE: SeedCatalog | None = None
_DEFAULT_CATALOG_LOCK = asyncio.Lock()


async def _default_catalog() -> SeedCatalog:
    """种子目录进程内惰性单例（load_seed_catalog 为 rdflib 同步解析 → asyncio.to_thread，
    standards/01 §2.5 禁同步 IO 混入请求路径）。"""
    global _DEFAULT_CATALOG_CACHE
    if _DEFAULT_CATALOG_CACHE is None:
        async with _DEFAULT_CATALOG_LOCK:
            if _DEFAULT_CATALOG_CACHE is None:  # 双重检查：并发首调只解析一次
                _DEFAULT_CATALOG_CACHE = await asyncio.to_thread(load_seed_catalog)
    return _DEFAULT_CATALOG_CACHE


async def rewrite(query: str, *, catalog: SeedCatalog | None = None) -> tuple[str, str] | None:
    r"""术语归一受限改写（M3 纠错，§3 ⑤；改写不是自由生成——只认种子目录命中）。

    规则（v1 受限版）：扫描查询中的 ``\w+`` 连续段（≥2 字符、非纯数字、不在否定词表），逐段
    两级归一（方向均为「查询词 → 规范标签」，如「配变」→「配电变压器」）：

    - 一级（match_seed_class 包含命中，kb_extraction 一级对齐只读复用）：``\w+`` 段与规范标签
      存在字面包含关系且尚未等于规范术语（如「变压」⊂「变压器」）→ 替换该段为规范标签；
      「标签 ⊂ 查询词」（规范术语已在查询中，如「馈线F001」含「馈线」）不改写——避免把含
      编号的实体词截断；
    - 二级（别名/简称启发式，match_seed_class 未命中时）：查询词的字符**按序**含于某规范标签
      （如「配变」的 配…变 按序含于「配电变压器」——中文「取首字+特征字」构词的简称）→
      替换该段为规范标签。按类声明序取首个命中（确定性）。

    只取最左一个可归一段（v1 单术语改写）。返回 (改写后查询, 依据="term_alias:<规范标签>")；
    无归一点或改写结果与原查询相同 → None（调用方据此降级，不空转重查同一查询）。
    ``catalog``=注入种子目录（测试/调用方装配用；None=懒加载 seeds/power_seed.ttl 进程内单例）。
    简称启发式为 PoC 档占位——正式别名以术语表（主文档 §4.3#3 aliases）承载后取代。
    """
    cat = catalog if catalog is not None else await _default_catalog()
    for term in _TERM_RE.findall(query):
        if len(term) < 2 or term.isdigit() or term in _REWRITE_BLOCKLIST:
            continue
        norm_term = term.lower()  # \w+ 段无空白，lower 即 kb_extraction._norm 语义
        target = _containment_target(norm_term, cat)  # 一级：字面包含方向裁决
        if target is None:
            target = _subsequence_target(norm_term, cat)  # 二级：别名/简称（字符按序包含）
        if target is None:
            continue
        rewritten = query.replace(term, target, 1)  # 最左一处（与扫描序一致）
        if rewritten != query:  # 红线：改写结果必须与原查询不同才返回
            return rewritten, f"term_alias:{target}"
    return None


def _containment_target(norm_term: str, catalog: SeedCatalog) -> str | None:
    """一级字面包含归一（match_seed_class 命中 + 方向裁决）：返回规范标签或 None。

    match_seed_class 的 contains 命中不分方向——「查询词 ⊂ 标签」=简称待归一（返回标签）；
    「标签 ⊂ 查询词」=规范术语已在查询中（None，不改写）；exact=已是规范术语（None）。
    """
    hit = match_seed_class(norm_term, catalog)
    if hit is None:
        return None
    iri, rule = hit
    if rule == "exact":
        return None
    label = _label_by_iri(catalog, iri)
    if label is not None and norm_term in label.lower() and norm_term != label.lower():
        return label
    return None


def _subsequence_target(norm_term: str, catalog: SeedCatalog) -> str | None:
    """二级别名/简称归一（match_seed_class 未命中的简称段）：查询词字符按序含于规范标签。"""
    for _iri, label, _local in catalog.classes:
        norm_label = label.lower()
        if norm_term == norm_label:
            continue
        it = iter(norm_label)
        if all(ch in it for ch in norm_term):  # 按序子序列判定（字符可跳、不可乱序）
            return label
    return None


def _label_by_iri(catalog: SeedCatalog, iri: str) -> str | None:
    return next((label for c_iri, label, _ in catalog.classes if c_iri == iri), None)


# ---------------------------------------------------------------- 编排（A0 服务端代跑）

SearchFn = Callable[[str], Awaitable[Sequence[SearchHit]]]


async def run_agentic_search(
    query: str,
    search_fn: SearchFn,
    *,
    max_rounds: int = 2,
    catalog: SeedCatalog | None = None,
) -> tuple[list[SearchHit], AgenticTrace]:
    """A0 服务端代跑主循环（§3 内核管线 v1 子集：①判别→③检索→④评级→⑤纠错）。

    流程：decide 判别——skip 则零检索直接返回（rounds 空，hits 空）；required 则循环
    ≤``max_rounds`` 轮：search（第 2 轮起为 rewrite_search，携带上一轮改写依据）→ grade →
    pass 即返回；fail 且可改写 → 术语归一改写进入下一轮；fail 且不可改写（rewrite=None）
    或轮次用尽 → degraded="agentic_exhausted"（§5 场景 4：理由回传不编造）。改写只在失败
    轮之后发起，且不可改写即停——不空转重查同一查询。

    返回 (最后一轮 hits（即使未过评级，调用方结合 trace.degraded 呈现）, 全程 trace)。
    ``search_fn``=单轮检索回调（query → 融合降序 hits；调用方自持会话/召回实现，本函数
    不触碰 retrieval 层与会话）。``catalog``=注入种子目录（None=懒加载默认种子）。
    """
    decision, reason = decide(query)
    if decision == "retrieval_skipped":  # §5 场景 1：判别跳过，零召回成本
        return [], AgenticTrace(decision=decision, decision_reason=reason)

    rounds: list[AgenticRound] = []
    pending: tuple[str, str] | None = None  # (下一轮查询, 改写依据)：失败轮末计算，下一轮消费
    last_hits: list[SearchHit] = []
    for seq in range(1, max_rounds + 1):
        action: AgenticAction = "search" if seq == 1 else "rewrite_search"
        basis: str | None = pending[1] if pending is not None else None
        round_query = pending[0] if pending is not None else query
        pending = None
        hits = list(await search_fn(round_query))
        last_hits = hits
        verdict, grade_reason = grade(hits)
        rounds.append(
            AgenticRound(
                seq=seq,
                action=action,
                query=round_query,
                rewrite_basis=basis,
                grade=verdict,
                grade_reason=grade_reason,
            )
        )
        if verdict == "pass":
            return hits, AgenticTrace(decision=decision, decision_reason=reason, rounds=rounds)
        if seq == max_rounds:
            break  # 纠错轮用尽（§2.3 红线 max_rounds ≤ 2）
        rewritten = await rewrite(round_query, catalog=catalog)
        if rewritten is None:
            break  # 不可归一改写 → 无从纠错，直接降级
        pending = rewritten

    return last_hits, AgenticTrace(
        decision=decision, decision_reason=reason, rounds=rounds, degraded="agentic_exhausted"
    )
