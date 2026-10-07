"""飞轮转化环 harvest 管道（docs/Agent/19 §5 批 F2/B6：会话反馈 → 候选场景草稿）。

三环中的第二环（19 §5 裁决）：采集环（前端 W9+后端 B5 落 ``session_feedback``）产出
用户信号第一落点；本模块从窗口内带 feedback 的 run 收割候选场景——

- **正例**（outcome=completed 且 tags 无否定）→ golden 候选；**负例**（outcome=
  failed/partial，或用户纠错文本非空，或带否定 tag）→ regression 候选（纠错优先于
  完成态——用户否定是最强信号）；
- 每候选产场景 YAML 草稿落 ``suites/agent-core/scenarios/proposed/``（目录即审核
  队列，**不进正式套件**——loader 按非递归 glob 拾取 scenarios/*.yaml，proposed
  子目录天然隔离；草稿必须过 runner.load_scenario_specs 同款白名单校验防污染）；
- **脱敏模板化**：用户专有名词（UUID/邮箱/IP/绝对路径四类）保守正则替换为
  ``⟪USER_n⟫`` 占位（同一原词→同号占位，保留任务结构不保留隐私，19 §5 裁决）；
- **断言键只引既有指标白名单**（runner._ALLOWED_METRIC_KEYS）：任务成功指标以新口径
  ``task_outcome_user_reported``（metrics.py ⑦，已登记白名单）承载；regression 另引
  ``invalid_retry_count <= 2``（内核循环防护界，metrics.invalid_retry 口径）；
- **回流环挂钩**（19 §5 第 3 环）：负例场景数>0 → quality_regression 注册**建议**
  （复用 orsi_link 建议输出形态，face=O4 技能面 / track=shortgap 常规缺口轨——用户
  纠错=常规缺口信号）；只建议不注册（宪法 3：候选非成品，注册必经人工确认端点）。

session_feedback 协议兼容（采集批 F1 未合入期）：缺省源=PG 直读 ``session_feedback``
表（§5 字段 run_id/outcome/tags/correction_text + 协议扩展 task_prompt/created_at）；
表未建（F1 未合）→ 显式 :class:`TableMissingError` 如实落档零产出，不静默不臆造；
``--feedback-json`` 提供同形 stub（本地 fake 读取/演练面）——F1 合入后同一管道对真表
自动生效（tests/benchmarks/test_flywheel_harvest.py 真库用例证协议）。

用法（仓库根）::

    python benchmarks/flywheel/harvest.py --since 2026-10-07
    python benchmarks/flywheel/harvest.py --since 2026-10-07 --feedback-json stub.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]  # benchmarks/flywheel/harvest.py → 仓库根
_BENCH_ROOT = _REPO_ROOT / "benchmarks"
if str(_REPO_ROOT) not in sys.path:  # 直跑形态（python benchmarks/flywheel/harvest.py）补仓库根——services 导入面
    sys.path.insert(0, str(_REPO_ROOT))

# 结果回链（orsi 建议 evidence.benchmark_ref 与 SUMMARY 头共用）
BENCHMARK_REF = "docs/Agent/19-通用Agent前后端对标与评测飞轮设计 §5（批 F2/B6，主仓本地）"

# 场景名/草稿文件前缀与断言口径
SCENARIO_PREFIX = "flywheel"
KIND_GOLDEN = "golden"
KIND_REGRESSION = "regression"
KIND_SKIP = "skip"
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_PARTIAL = "partial"
_KNOWN_OUTCOMES = (OUTCOME_COMPLETED, OUTCOME_FAILED, OUTCOME_PARTIAL)
# regression 断言：内核循环防护有界性（abort_threshold=2 → invalid_retry_count ≤ 2，
# metrics.invalid_retry 口径：invalid_retry_count = 调用数−1 ≤ 阈值）
REGRESSION_RETRY_ASSERT = {"metric": "invalid_retry_count", "op": "<=", "value": 2}


# ---------------------------------------------------------------------------
# ① 可变参数（D2 纪律：一切可变参数走 Settings；env 前缀 BENCH_FLYWHEEL_）
# ---------------------------------------------------------------------------


class FlywheelSettings(BaseSettings):
    """harvest 管道可变参数（env 前缀 BENCH_FLYWHEEL_，可 .env 注入）。"""

    model_config = SettingsConfigDict(env_prefix="BENCH_FLYWHEEL_", env_file=".env", extra="ignore", frozen=True)

    # 否定 tag 词表（§5「tags 无否定」判定面——采集批 F1 合入后按其词表对齐登记）
    negative_tags: frozenset[str] = frozenset(
        {"wrong", "incorrect", "unhelpful", "incomplete", "irrelevant", "hallucination", "failed", "bad"}
    )
    # 脱敏占位模板（{n}=占位序号；同一原词→同号）
    placeholder_format: str = "⟪USER_{n}⟫"
    # 草稿落点=审核队列目录（proposed/ 不进正式套件：loader 非递归 glob 天然隔离）
    proposed_dir: Path = _BENCH_ROOT / "suites" / "agent-core" / "scenarios" / "proposed"
    # harvest 结果落点根（results/flywheel/<since 日期>/）
    results_root: Path = _BENCH_ROOT / "results"
    # PG 源表名（F1 采集批约定名；标识符白名单校验后拼接）
    feedback_table: str = "session_feedback"


# ---------------------------------------------------------------------------
# ② 脱敏模板化（保守正则替换：UUID/邮箱/IP/绝对路径 → ⟪USER_n⟫）
# ---------------------------------------------------------------------------

# UUID：严格连字符形；前后界排除 hex/-——CJK 邻接（「线路03de…」）不漏替，英文词内
# 粘连（abc03de…）保守跳过（歧义不臆判）
_UUID_RE = re.compile(
    r"(?<![0-9A-Fa-f-])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f-])"
)
# 邮箱：标准形（贪婪，先于 IP/UUID 替换避免片段化）
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# IPv4：逐段 ≤255；前后界仅排除 ASCII 字母/数字/点——CJK 邻接（「地址192.168…」）不漏替，
# 版本号（v1.2.3.4）与多段点串（1.2.3.4.5）保守不误伤
_IPV4_RE = re.compile(
    r"(?<![A-Za-z0-9.])(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}(?![A-Za-z0-9.])"
)
# Windows 绝对路径：盘符+分隔符起（词中盘符如 SEA:/x 保守跳过）；分隔符后排除空白与
# 文件名非法字符——含空格路径在空格处截断（保守=宁少替，尾标点由回调剥离保留）
_WIN_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s<>|\"'`*?:\r\n]+")
# POSIX 绝对路径：锚定常见根目录词（home/Users/var/tmp/opt/etc/usr/mnt/data/srv/root/
# workspace）——日期分数等斜杠串不误伤；前后界排除 ASCII 连续词字符
_POSIX_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.~/])/(?:home|Users|var|tmp|opt|etc|usr|mnt|data|srv|root|workspace)(?:/[\w.\-]+)+"
)

# 替换序：路径最先行（路径体内可含 email/uuid 片段，整体替换优先于片段）
_SANITIZE_PASS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("path", _WIN_PATH_RE),
    ("path", _POSIX_PATH_RE),
    ("email", _EMAIL_RE),
    ("uuid", _UUID_RE),
    ("ip", _IPV4_RE),
)
# 路径尾标点剥离集（匹配贪婪吞进的句读还原到占位符之后，不丢原文标点）
_TRAIL_PUNCT = ".,;:、，。；：）)】》〉\"'“”‘’"


def sanitize_text(text: str, *, placeholder_format: str = "⟪USER_{n}⟫") -> tuple[str, dict[str, int]]:
    """脱敏模板化：四类专有名词 → ⟪USER_n⟫ 占位。

    返回 (脱敏后文本, 分类替换计数)——计数按替换发生次数（同一原词多次出现逐次计）；
    占位编号按「原词→同号」分配（同一 UUID/邮箱全文同号，保留共指结构）。
    保守语义：宁可漏替不误伤（版本号/多段点串/词中粘连不替换），漏替风险由审核队列
    人工终审兜底（宪法 3）。
    """
    counts = {"path": 0, "email": 0, "uuid": 0, "ip": 0}
    alloc: dict[str, str] = {}
    state = {"n": 0}

    def placeholder_for(raw: str) -> str:
        if raw not in alloc:
            state["n"] += 1
            alloc[raw] = placeholder_format.format(n=state["n"])
        return alloc[raw]

    def make_repl(category: str) -> Callable[[re.Match[str]], str]:
        def _repl(match: re.Match[str]) -> str:
            token = match.group(0)
            if category == "path":  # 路径贪婪匹配吞进的尾标点还原（不丢句读）
                stripped = token.rstrip(_TRAIL_PUNCT)
                tail = token[len(stripped) :]
            else:
                stripped, tail = token, ""
            counts[category] += 1
            return placeholder_for(stripped) + tail

        return _repl

    out = text
    for category, pattern in _SANITIZE_PASS:
        out = pattern.sub(make_repl(category), out)
    return out, counts


# ---------------------------------------------------------------------------
# ③ 反馈源协议（session_feedback 协议兼容面：F1 未合入期 stub 可跑，合入后自动生效）
# ---------------------------------------------------------------------------


class TableMissingError(RuntimeError):
    """session_feedback 表不存在（采集批 F1 未合入）——协议兼容期预期态，非错误。"""


@dataclass
class FeedbackRow:
    """§5 字段口径（run_id/outcome/tags/correction_text）+ 协议扩展（task_prompt/created_at）。

    task_prompt=该 run 的原始用户任务文本（真表由 F1 冗余落列或 JOIN 供出；stub 直供）；
    二者皆缺且无纠错文本的候选无任务信号，管道跳过并计数（不臆造）。
    """

    run_id: str
    outcome: str | None = None
    tags: list[str] = field(default_factory=list)
    correction_text: str | None = None
    task_prompt: str | None = None
    created_at: datetime | None = None


class FeedbackSource(Protocol):
    """反馈源协议：PG 直读（缺省）与 stub JSON（演练/测试）同形互换。"""

    async def read(self, since: datetime) -> list[FeedbackRow]: ...

    def describe(self) -> str: ...


def _coerce_tags(raw: Any) -> list[str]:
    """tags 列协议宽收：list 直收；str 尝试 JSON 解析（JSONB/text 两态）；None→[]。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return [raw] if raw.strip() else []
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return [str(parsed)] if str(parsed).strip() else []
    return [str(raw)]


class JsonFeedbackReader:
    """stub 源：``--feedback-json`` 文件（同形协议本地 fake 读取——演练/测试面）。

    形状：反馈行列表，或 {"feedback": [...]} / {"rows": [...]} 包裹（宽收不窄拒）。
    行字段=FeedbackRow 同名键（§5 四字段+协议扩展两字段）。
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def describe(self) -> str:
        return f"stub-json:{self._path}"

    async def read(self, since: datetime) -> list[FeedbackRow]:
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            rows_raw = next((payload[key] for key in ("feedback", "rows") if isinstance(payload.get(key), list)), [])
        elif isinstance(payload, list):
            rows_raw = payload
        else:
            rows_raw = []
        out: list[FeedbackRow] = []
        for item in rows_raw:
            if not isinstance(item, dict) or not str(item.get("run_id", "")).strip():
                continue  # 无 run_id 的行不构成候选（如实跳过，非静默——窗口计数不含它）
            created = item.get("created_at")
            out.append(
                FeedbackRow(
                    run_id=str(item["run_id"]),
                    outcome=item.get("outcome"),
                    tags=_coerce_tags(item.get("tags")),
                    correction_text=item.get("correction_text"),
                    task_prompt=item.get("task_prompt"),
                    created_at=datetime.fromisoformat(str(created)) if created else None,
                )
            )
        return [row for row in out if row.created_at is None or row.created_at >= since]


class PgFeedbackReader:
    """PG 直读源（缺省）：``SELECT … FROM session_feedback WHERE created_at >= :since``。

    协议兼容三级降级：表不存在 → TableMissingError（F1 未合入期预期态）；
    task_prompt 列不存在（F1 首版未冗余该列）→ §5 最小字段集重查（task_prompt=None）；
    都在 → 全字段读。表名经标识符白名单校验后拼接（非参数位，防注入）。
    """

    _TABLE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

    def __init__(self, settings: Any, *, table: str = "session_feedback") -> None:
        if not self._TABLE_NAME_RE.fullmatch(table):
            raise ValueError(f"非法表名标识符: {table!r}")
        self._settings = settings
        self._table = table

    def describe(self) -> str:
        return f"pg:{self._table}"

    # PG SQLSTATE（驱动经 sqlalchemy ProgrammingError.orig.sqlstate 透出）：
    _SQLSTATE_UNDEFINED_TABLE = "42P01"
    _SQLSTATE_UNDEFINED_COLUMN = "42703"

    async def read(self, since: datetime) -> list[FeedbackRow]:
        from sqlalchemy import text
        from sqlalchemy.exc import ProgrammingError
        from sqlalchemy.ext.asyncio import create_async_engine

        sql = (
            f"SELECT run_id, outcome, tags, correction_text, task_prompt, created_at "  # noqa: S608 ——表名白名单校验
            f"FROM {self._table} WHERE created_at >= :since ORDER BY created_at"
        )
        sql_minimal = (
            f"SELECT run_id, outcome, tags, correction_text, NULL AS task_prompt, created_at "
            f"FROM {self._table} WHERE created_at >= :since ORDER BY created_at"
        )

        def sqlstate(exc: ProgrammingError) -> str | None:
            return getattr(getattr(exc, "orig", None), "sqlstate", None)

        engine = create_async_engine(self._settings.pg_dsn, pool_pre_ping=True)
        try:
            async with engine.connect() as conn:
                try:
                    result = await conn.execute(text(sql), {"since": since})
                except ProgrammingError as exc:
                    if sqlstate(exc) == self._SQLSTATE_UNDEFINED_COLUMN:
                        # task_prompt 列不存在（F1 首版形态）→ §5 最小字段集重查（协议降级；
                        # psycopg 事务内报错后须先回滚才可续查）
                        await conn.rollback()
                        result = await conn.execute(text(sql_minimal), {"since": since})
                    elif sqlstate(exc) == self._SQLSTATE_UNDEFINED_TABLE:
                        raise TableMissingError(
                            f"{self._table} 表不存在——采集批（F1）未合入，协议兼容零产出（合入后本管道自动生效）"
                        ) from exc
                    else:
                        raise
        finally:
            await engine.dispose()
        rows: list[FeedbackRow] = []
        for row in result.mappings():
            created = row.get("created_at")
            rows.append(
                FeedbackRow(
                    run_id=str(row["run_id"]),
                    outcome=row.get("outcome"),
                    tags=_coerce_tags(row.get("tags")),
                    correction_text=row.get("correction_text"),
                    task_prompt=row.get("task_prompt"),
                    created_at=created if isinstance(created, datetime) else None,
                )
            )
        return rows


# ---------------------------------------------------------------------------
# ④ 正负例分类（19 §5：正例=完成+无否定→golden；负例=失败/纠错→regression）
# ---------------------------------------------------------------------------


def classify_feedback(row: FeedbackRow, *, negative_tags: frozenset[str]) -> tuple[str, str]:
    """反馈行 → (kind, reason)；kind ∈ {golden, regression, skip}。

    裁决口径（19 §5）：任务失败（failed/partial）、用户纠错文本非空、否定 tag 三者
    任一命中即 regression（纠错优先于 completed——用户否定是最强信号）；
    completed 且无否定才 golden；outcome 缺失/非三元词表 → skip 如实计数。
    """
    outcome = (row.outcome or "").strip().lower()
    correction = (row.correction_text or "").strip()
    if outcome not in _KNOWN_OUTCOMES:
        return (KIND_SKIP, f"unknown_outcome:{row.outcome!r}")
    tags = {str(tag).strip().lower() for tag in row.tags}
    negated = negative_tags & tags
    if outcome in (OUTCOME_FAILED, OUTCOME_PARTIAL):
        return (KIND_REGRESSION, f"outcome={outcome}")
    if correction:
        return (KIND_REGRESSION, "user_correction")
    if negated:
        return (KIND_REGRESSION, f"negative_tag:{sorted(negated)[0]}")
    if outcome == OUTCOME_COMPLETED:
        return (KIND_GOLDEN, "completed_clean")
    return (KIND_SKIP, "missing_outcome")  # pragma: no cover ——outcome 已在三元词表，防御分支


def _short_hash(run_id: str) -> str:
    """run_id 短哈希（sha256 前 12 hex——场景名脱原值，草稿名不含用户数据）。"""
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]


def scenario_name(kind: str, short: str) -> str:
    return f"{SCENARIO_PREFIX}:{kind}:{short}"


# ---------------------------------------------------------------------------
# ⑤ 草稿构建与 schema 复检（runner.load_scenario_specs 同款白名单校验=防污染闸）
# ---------------------------------------------------------------------------


def build_asserts(kind: str) -> list[dict[str, Any]]:
    """草稿断言（键全部在 runner._ALLOWED_METRIC_KEYS 白名单内）：

    - golden：重跑后用户口径任务完成（task_outcome_user_reported == true）；
    - regression：修复后任务完成 + 无效重试有界（invalid_retry_count ≤ 2，内核
      循环防护界——metrics.invalid_retry 口径 invalid_retry_count=调用数−1）。
    """
    outcome_assert = {"metric": "task_outcome_user_reported", "op": "==", "value": True}
    return [outcome_assert] if kind == KIND_GOLDEN else [outcome_assert, dict(REGRESSION_RETRY_ASSERT)]


def build_draft_yaml(
    *,
    scenario: str,
    kind: str,
    reason: str,
    run_id: str,
    task_prompt: str,
    reported_outcome: str,
    correction: str | None,
    harvested_at: str,
) -> str:
    """候选 → 场景 YAML 草稿文本（手织+JSON 字符串字面量=合法 YAML，unicode 不转义）。

    params 必带 task_prompt（已脱敏模板化）与 expected_outcome（重跑通过口径恒=
    completed）；reported_outcome/correction 留用户信号原貌（脱敏后）供审核归因。
    """
    params: dict[str, Any] = {
        "task_prompt": task_prompt,
        "expected_outcome": OUTCOME_COMPLETED,
        "reported_outcome": reported_outcome,
        "source_run_id": run_id,
        "flywheel_kind": kind,
        "harvested_at": harvested_at,
    }
    if correction:
        params["user_correction"] = correction
    lines = [
        "# 飞轮转化环候选草稿（docs/Agent/19 §5 批 F2）——审核队列：未过审不进正式套件（宪法 3：候选非成品）",
        f"# 源 run: {run_id} | kind: {kind} | 归因: {reason} | harvest: {harvested_at}",
        "# 脱敏说明：用户专有名词（UUID/邮箱/IP/绝对路径）已保守替换为 ⟪USER_n⟫ 占位（同词同号）",
        "version: 1",
        f"scenario: {json.dumps(scenario, ensure_ascii=False)}",
        "description: |",
        f"  飞轮 {kind} 候选：会话反馈收割（归因 {reason}）。golden=用户确认完成的任务回归样；",
        "  regression=用户纠错/失败样——修复后重跑应以 params.expected_outcome 收敛。",
        "params:",
    ]
    for key, value in params.items():
        lines.append(f"  {key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("asserts:")
    for item in build_asserts(kind):
        lines.append(f"  - metric: {item['metric']}")
        lines.append(f"    op: {json.dumps(item['op'], ensure_ascii=False)}")
        lines.append(f"    value: {json.dumps(item['value'], ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def load_agent_core_runner() -> Any:
    """文件位加载 agent-core runner（目录名含连字符，run.py 同款 importlib 形态）——
    仅为复用其 load_scenario_specs 白名单校验（防污染闸与正式套件同一道门）。"""
    runner_path = _BENCH_ROOT / "suites" / "agent-core" / "runner.py"
    if not runner_path.is_file():
        raise FileNotFoundError(f"agent-core runner 不存在: {runner_path}")
    spec = importlib.util.spec_from_file_location("bench_flywheel_agent_core_runner", str(runner_path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def validate_drafts(proposed_dir: Path, expected_scenarios: list[str]) -> int:
    """草稿目录整体过 runner.load_scenario_specs 白名单校验；期望场景逐一在册即通过。

    校验语义与正式套件完全同款（schema+断言键白名单+操作符白名单）——目录内任何
    不合格 YAML（含既往队列内容）都会在此显式报错=防污染闸生效。返回通过校验的
    期望场景数（<len(expected) 即草稿未落盘，调用方报错）。
    """
    specs = load_agent_core_runner().load_scenario_specs(proposed_dir)
    missing = [name for name in expected_scenarios if name not in specs]
    if missing:
        raise RuntimeError(f"草稿未通过 schema 校验（防污染闸）: {missing}")
    return len(expected_scenarios)


# ---------------------------------------------------------------------------
# ⑥ orsi 挂钩建议（回流环：负例→quality_regression 注册建议；只建议不注册）
# ---------------------------------------------------------------------------


def build_orsi_suggestions(
    regression_candidates: list[HarvestCandidate],
    *,
    draft_rel_uri: dict[str, str],
    repo_root: Path,
) -> list[dict[str, Any]]:
    """负例候选 → quality_regression 注册建议列表（复用 orsi_link 建议输出形态）。

    face=O4 技能面 / track=shortgap 常规缺口轨（services/rsi/domain/orsi.py 轨道枚举；
    19 §5 回流环裁决：用户纠错=常规缺口信号，异于 eval 劣化的 critical 轨）。
    建议≠注册（auto_registered=False，宪法 3）：注册必经人工确认后走注册端点。
    """
    if not regression_candidates:
        return []
    from benchmarks.orsi_link import scenario_set_hash  # 场景集哈希口径与 orsi_link 同源

    pairs = [(repo_root / draft_rel_uri[c.scenario], {"scenario": c.scenario}) for c in regression_candidates]
    set_hash = scenario_set_hash(pairs)
    suggestions: list[dict[str, Any]] = []
    for cand in regression_candidates:
        suggestions.append(
            {
                "suggestion_type": "orsi_capability_registration",
                "auto_registered": False,
                "proposal": {
                    "name": f"quality_regression:{SCENARIO_PREFIX}.{cand.short}",
                    "face": "O4",  # 17 篇 §3 裁决：agent-core 归属技能面
                    "status": "candidate",  # 宪法 3：候选非成品
                    "version": "1.0.0",
                    "source_channel": "L0",
                    "source_face_track": "shortgap",  # 19 §5：用户纠错=常规缺口轨
                },
                "evidence": {
                    "evidence_uri": draft_rel_uri[cand.scenario],
                    "run_status": "proposed",  # 草稿在审核队列，未实跑
                    "smoke": None,
                    "benchmark_ref": BENCHMARK_REF,
                    "key_metrics": {},  # 场景未实跑无实测指标——注册时以重跑结果补（不臆造）
                    "source_run_id": cand.row.run_id,
                    "classification_reason": cand.reason,
                },
                "capability_fingerprint": hashlib.sha256(
                    f"flywheel-scenarios:{set_hash}:{cand.scenario}".encode()
                ).hexdigest(),
                "fingerprint_note": (
                    "飞轮场景集哈希（orsi_link.scenario_set_hash 同口径：场景名排序 \\x1f join sha256）；"
                    "非注册面算法——正式注册时由 services/rsi 业务层重算，本值仅供人工比照"
                ),
                "rationale": (
                    f"用户负反馈信号（{cand.reason}）→ quality_regression 常规缺口轨注册建议"
                    f"（{BENCHMARK_REF} 回流环：track=shortgap，face=O4 技能面）；候选非成品——注册必经人工确认"
                ),
            }
        )
    return suggestions


# ---------------------------------------------------------------------------
# ⑦ 管道主体与产物落盘
# ---------------------------------------------------------------------------


@dataclass
class HarvestCandidate:
    """一个通过分类的候选：源行+脱敏产物+场景名（草稿在审核队列的锚）。"""

    row: FeedbackRow
    kind: str
    reason: str
    short: str
    scenario: str
    task_prompt_sanitized: str
    correction_sanitized: str | None
    sanitize_counts: dict[str, int]

    @property
    def reported_outcome(self) -> str:
        return (self.row.outcome or "").strip().lower()


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _rel_uri(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(_REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(resolved).replace("\\", "/")


def _empty_report(*, since: datetime, harvested_at: str, source_desc: str, note: str) -> dict[str, Any]:
    return {
        "schema": "flywheel.harvest/v1",
        "benchmark_ref": BENCHMARK_REF,
        "since": since.isoformat(),
        "harvested_at": harvested_at,
        "feedback_source": source_desc,
        "note": note,
        "counts": {
            "feedback_total": 0,
            "golden": 0,
            "regression": 0,
            "skipped_unknown_outcome": 0,
            "skipped_empty_signal": 0,
            "drafts_written": 0,
            "drafts_validated": 0,
            "sanitization": {"uuid": 0, "email": 0, "ip": 0, "path": 0, "total": 0},
        },
        "candidates": [],
        "orsi_suggestions": [],
    }


def run_harvest_sync(*, settings: FlywheelSettings, since: datetime, source: FeedbackSource) -> dict[str, Any]:
    """同步外壳（CLI 用）：固定 Selector 循环策略后跑 async 管道（run.py 同款先例）。"""
    if sys.platform == "win32":  # psycopg 异步要求 Selector 循环（须在 asyncio.run 前固定）
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(run_harvest(settings=settings, since=since, source=source))


async def run_harvest(*, settings: FlywheelSettings, since: datetime, source: FeedbackSource) -> dict[str, Any]:
    """harvest 主管道：读反馈→分类→脱敏→草稿落队列→schema 复检→orsi 建议→产物落盘。"""
    harvested_at = _utcnow_iso()
    source_desc = source.describe()
    try:
        rows = await source.read(since)
    except TableMissingError as exc:
        report = _empty_report(since=since, harvested_at=harvested_at, source_desc=source_desc, note=str(exc))
        _write_artifacts(report, settings=settings, since=since)
        return report

    report = _empty_report(since=since, harvested_at=harvested_at, source_desc=source_desc, note=None)
    report["counts"]["feedback_total"] = len(rows)
    candidates: list[HarvestCandidate] = []
    for row in rows:
        kind, reason = classify_feedback(row, negative_tags=settings.negative_tags)
        if kind == KIND_SKIP:
            report["counts"]["skipped_unknown_outcome"] += 1
            continue
        # 任务信号面：golden 只认任务文本；regression 可退化为纠错文本（纠错即任务语境）
        basis = row.task_prompt if kind == KIND_GOLDEN else (row.task_prompt or row.correction_text)
        if not (basis or "").strip():
            report["counts"]["skipped_empty_signal"] += 1
            continue
        prompt_san, counts = sanitize_text(basis.strip(), placeholder_format=settings.placeholder_format)
        correction_san = None
        if kind == KIND_REGRESSION and (row.correction_text or "").strip():
            correction_san, more = sanitize_text(
                row.correction_text.strip(), placeholder_format=settings.placeholder_format
            )
            for key, value in more.items():
                counts[key] = counts.get(key, 0) + value
        short = _short_hash(row.run_id)
        candidates.append(
            HarvestCandidate(
                row=row,
                kind=kind,
                reason=reason,
                short=short,
                scenario=scenario_name(kind, short),
                task_prompt_sanitized=prompt_san,
                correction_sanitized=correction_san,
                sanitize_counts=counts,
            )
        )
        report["counts"][kind] += 1

    # 草稿落审核队列（proposed/；目录即队列，非递归 loader 天然隔离不进正式套件）
    settings.proposed_dir.mkdir(parents=True, exist_ok=True)
    draft_rel_uri: dict[str, str] = {}
    for cand in candidates:
        draft_path = settings.proposed_dir / f"{SCENARIO_PREFIX}-{cand.kind}-{cand.short}.yaml"
        draft_path.write_text(
            build_draft_yaml(
                scenario=cand.scenario,
                kind=cand.kind,
                reason=cand.reason,
                run_id=cand.row.run_id,
                task_prompt=cand.task_prompt_sanitized,
                reported_outcome=cand.reported_outcome,
                correction=cand.correction_sanitized,
                harvested_at=harvested_at,
            ),
            encoding="utf-8",
        )
        draft_rel_uri[cand.scenario] = _rel_uri(draft_path)
    report["counts"]["drafts_written"] = len(candidates)
    expected = [cand.scenario for cand in candidates]
    if expected:
        report["counts"]["drafts_validated"] = validate_drafts(settings.proposed_dir, expected)
    else:
        report["counts"]["drafts_validated"] = 0

    # 脱敏计数汇总（分类展示：uuid/email/ip/path 四类）
    for cand in candidates:
        for key, value in cand.sanitize_counts.items():
            report["counts"]["sanitization"][key] = report["counts"]["sanitization"].get(key, 0) + value
    report["counts"]["sanitization"]["total"] = sum(report["counts"]["sanitization"].values())

    # 回流环挂钩：负例>0 → quality_regression 注册建议（face=O4，track=shortgap）
    regression = [cand for cand in candidates if cand.kind == KIND_REGRESSION]
    report["orsi_suggestions"] = build_orsi_suggestions(regression, draft_rel_uri=draft_rel_uri, repo_root=_REPO_ROOT)

    # 候选清单（traceability：run→候选→草稿→建议全链可溯）
    report["candidates"] = [
        {
            "run_id": cand.row.run_id,
            "kind": cand.kind,
            "reason": cand.reason,
            "reported_outcome": cand.reported_outcome,
            "scenario": cand.scenario,
            "draft": draft_rel_uri[cand.scenario],
            "asserts": build_asserts(cand.kind),
            "sanitize_counts": cand.sanitize_counts,
        }
        for cand in candidates
    ]
    _write_artifacts(report, settings=settings, since=since)
    return report


def _write_artifacts(report: dict[str, Any], *, settings: FlywheelSettings, since: datetime) -> tuple[Path, Path]:
    """harvest.json（当日窗口产物）+ SUMMARY.md（追加式，run.py SUMMARY 同纪律）。"""
    date_dir = settings.results_root / "flywheel" / since.date().isoformat()
    date_dir.mkdir(parents=True, exist_ok=True)
    harvest_json = date_dir / "harvest.json"
    harvest_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    counts = report["counts"]
    sanitize = counts["sanitization"]
    lines = [
        "",
        f"## {report['harvested_at']} since={report['since']} source={report['feedback_source']}",
        "",
        f"- 反馈窗口 {counts['feedback_total']} 条 → golden={counts['golden']} regression={counts['regression']}"
        f"（skip：未知 outcome={counts['skipped_unknown_outcome']} 无任务信号={counts['skipped_empty_signal']}）",
        f"- 脱敏替换：uuid={sanitize.get('uuid', 0)} email={sanitize.get('email', 0)} "
        f"ip={sanitize.get('ip', 0)} path={sanitize.get('path', 0)} 合计={sanitize.get('total', 0)}",
        f"- 草稿落 proposed/（审核队列）：{counts['drafts_written']} 份，schema 白名单复检通过 "
        f"{counts['drafts_validated']}/{counts['drafts_written']}；orsi 注册建议 "
        f"{len(report['orsi_suggestions'])} 条（face=O4 track=shortgap，只建议不注册）",
    ]
    if report.get("note"):
        lines.append(f"> note: {report['note']}")
    summary_md = date_dir / "SUMMARY.md"
    with summary_md.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return harvest_json, summary_md


# ---------------------------------------------------------------------------
# ⑧ CLI
# ---------------------------------------------------------------------------


def parse_since(raw: str) -> datetime:
    """--since 解析：ISO 日期或日期时间；无时区按 UTC 归一（PG timestamptz 比较安全）。"""
    value = datetime.fromisoformat(raw.strip())
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmarks.flywheel.harvest",
        description="飞轮转化环 harvest：会话反馈窗口 → 候选场景草稿（审核队列）+ orsi 注册建议",
    )
    parser.add_argument("--since", required=True, help="窗口起点（ISO 日期/日期时间，如 2026-10-07）")
    parser.add_argument(
        "--feedback-json",
        default=None,
        help="stub 反馈源（同形协议本地 fake 读取；缺省直读 PG session_feedback 表——表缺失零产出）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = FlywheelSettings()
    try:
        since = parse_since(args.since)
    except ValueError as exc:
        print(f"[flywheel] --since 解析失败（须 ISO 日期/日期时间）: {exc}", file=sys.stderr)
        return 2
    if args.feedback_json:
        stub_path = Path(args.feedback_json)
        if not stub_path.is_file():
            print(f"[flywheel] stub 反馈文件不存在: {stub_path}", file=sys.stderr)
            return 2
        source: FeedbackSource = JsonFeedbackReader(stub_path)
    else:
        from services.platform.config import Settings as PlatformSettings

        source = PgFeedbackReader(PlatformSettings(), table=settings.feedback_table)

    report = run_harvest_sync(settings=settings, since=since, source=source)
    counts = report["counts"]
    print(
        json.dumps(
            {
                "since": report["since"],
                "source": report["feedback_source"],
                "feedback_total": counts["feedback_total"],
                "golden": counts["golden"],
                "regression": counts["regression"],
                "skipped": counts["skipped_unknown_outcome"] + counts["skipped_empty_signal"],
                "drafts": counts["drafts_written"],
                "sanitization_total": counts["sanitization"]["total"],
                "orsi_suggestions": len(report["orsi_suggestions"]),
                "note": report["note"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    date_dir = settings.results_root / "flywheel" / since.date().isoformat()
    print(f"\nharvest.json: {date_dir / 'harvest.json'}")
    print(f"SUMMARY:      {date_dir / 'SUMMARY.md'}")
    print(f"审核队列:     {settings.proposed_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
