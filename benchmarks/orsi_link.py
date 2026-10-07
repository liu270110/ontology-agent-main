"""ORSI 挂钩 v2（docs/Agent/16 §3 + 17 篇 §3）：suite 结果 JSON → ORSI capability 注册。

两种模式（17 篇 §3「建议→实装闭环」）：

1. **建议模式（v1 形态，缺省）**：只产出建议 JSON，不注册——人工确认后走注册端点。
2. **注册模式（v2 ``--register``）**：release-eval 后真实调用
   ``POST /api/v1/orsi/capabilities``（进程内 ``httpx.ASGITransport`` + 网关同款中间件栈
   + 真 JWT（rsi:write scope）+ 真 PG 会话——先例照 tests/rsi/test_orsi_registry.py
   api_client 形态），按套件归属注册 capability（17 篇 §3 裁决：agent-core=O4 技能面 /
   rag+intent=O7 检索策略 / ontology-scale=O2 行动类语义），注册 payload 经
   ``promotion_evidence`` 携带五字段证据链 ``{eval_tag, scenario_hash, metrics_digest,
   baseline_digest(上一 tag), version_diff_uri}``——**capability 注册成为版本迭代质量
   证据链**（17 篇 §3.2）。

v2 机制化要点（17 篇 §3）：

- **幂等**（§3.2 迭代 re-run）：同 ``tag+scenario_hash`` 重复注册 → 查注册表既有行
  复用跳过（不灌注册表）；``scenario_hash`` 编入 name 使场景集变更即新指纹（可并存）；
- **劣化反哺**（§3.4 自我迭代触发）：``--version-diff`` 提供 version_diff 产物时，
  任一核心指标 trend=↓ 相对降幅 ≥ 阈值（``BENCH_ORSI_REGRESSION_THRESHOLD``/CLI
  ``--regression-threshold``，缺省 5%）→ 额外注册 ``quality_regression:<metric>``
  capability（track=critical 缺口轨）——评测结果反哺 ORSI 缺口轨；
- **红线不动**：注册走候选（status=candidate，宪法 3）；promote 恒拒/apply 恒拒原样
  （promotion_evidence 仅承载证据，不激活任何迁移——领域层 docstring 权威）。

用法::

    # 建议（v1 兼容）
    python benchmarks/orsi_link.py --results benchmarks/results/agent-core/<date> --out suggestions.json
    # 注册（v2）：注册 eval 证据链 capability（+ 劣化触发 quality_regression）
    python benchmarks/orsi_link.py --results benchmarks/results/agent-core/<date> \\
        --register --tag v0.2.0-m4.7 --version-diff benchmarks/results/version_diff.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:  # 直跑形态（python benchmarks/orsi_link.py）补仓库根——services 导入面
    sys.path.insert(0, str(_REPO_ROOT))

# suite → ORSI face 归属（17 篇 §3 裁决 v2：agent-core=O4 技能面 / rag+intent=O7 检索
# 策略 / ontology-scale=O2 行动类语义；services/rsi/surfaces.py EvolutionSurface 封闭八面）。
_SUITE_FACE: dict[str, str] = {"agent-core": "O4", "rag": "O7", "intent": "O7", "ontology-scale": "O2"}
# 场景级面覆盖（runner.ORSI_FACE_SUGGESTION 同源；场景级优先于 suite 级——记忆交叉污染属 O6 记忆策略）
_SCENARIO_FACE: dict[str, str] = {"memory_cross_contamination": "O6"}


def _face_for(suite: str, scenario: str) -> str:
    return _SCENARIO_FACE.get(scenario, _SUITE_FACE.get(suite, "O4"))


# ---------------------------------------------------------------- 可变参数（D2 纪律：走 Settings）


class OrsiLinkSettings(BaseSettings):
    """orsi_link 注册模式可变参数（env 前缀 BENCH_ORSI_，可 .env 注入；CLI 参数可覆盖）。"""

    model_config = SettingsConfigDict(env_prefix="BENCH_ORSI_", env_file=".env", extra="ignore", frozen=True)

    # 劣化判定阈值（17 篇 §3.4：任一核心指标 trend=↓ 相对降幅 ≥ 阈值 → quality_regression）
    regression_threshold: float = Field(default=0.05, ge=0.0, le=1.0)
    # 评测注册归属租户（无 FK——首 27 表口径；固定值保证注册行归属可追溯、幂等查重同租户）
    tenant_id: str = "00000000-0000-0000-0000-00000000e105"
    # 注册主体 user（JWT sub；同无 FK 口径——评测自签 token 的确定性主体）
    user_id: str = "00000000-0000-0000-0000-00000000e106"
    # 注册用 token 有效期秒（一次性短签）
    token_ttl_seconds: int = Field(default=3600, ge=60)
    # 列表查重单页大小（幂等/baseline 检索面；face 过滤后单租户行数远小于此）
    list_page_size: int = Field(default=100, ge=1, le=100)


# ---------------------------------------------------------------- 结果装载与指纹（v1 口径延续）


def load_results(results_path: Path) -> list[tuple[Path, dict[str, Any]]]:
    """读结果：单文件或目录（目录=取 *.json，manifest 除外）；路径统一 resolve（evidence_uri 相对化用）。

    citation_only 件（公开榜单引用表，benchmarks/suites/rag/leaderboard_citations/，
    非实测数据）显式跳过——ORSI evidence 只收实测指标，文献引用不进能力注册建议。
    """
    results_path = results_path.resolve()
    if results_path.is_file():
        payload = json.loads(results_path.read_text(encoding="utf-8"))
        if payload.get("nature") == "citation_only":
            return []
        return [(results_path, payload)]
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(results_path.glob("*.json")):
        if path.name.endswith("manifest.json"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("nature") == "citation_only":
            continue
        out.append((path, payload))
    return out


def scenario_set_hash(pairs: list[tuple[Path, dict[str, Any]]]) -> str:
    """场景集哈希（16 篇 §3 口径：排序场景名 \\x1f join 后 sha256）。

    场景集变=哈希变=能力需重评（ORSI 参考依据的失效联动面）；注册模式下同值编入
    capability name（参与指纹）与 promotion_evidence.scenario_hash（证据链）。
    """
    scenario_set = sorted(result.get("scenario", "?") for _, result in pairs)
    return hashlib.sha256("\x1f".join(scenario_set).encode("utf-8")).hexdigest()


def key_metrics_of(pairs: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    """结果对 → 数值型指标快照（v1 evidence.key_metrics 同口径：场景名.指标名打平）。"""
    flat: dict[str, Any] = {}
    for _path, result in pairs:
        scenario = result.get("scenario", "?")
        metrics = result.get("metrics", {})
        for key in sorted(metrics):
            value = metrics[key]
            if isinstance(value, (int, float, bool)):
                flat[f"{scenario}.{key}"] = value
    return flat


def metrics_digest(key_metrics: dict[str, Any]) -> str:
    """指标摘要 sha256（canonical：sort_keys 紧凑 JSON——同指标集同摘要，可独立复算）。"""
    canonical = json.dumps(key_metrics, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_suggestions(pairs: list[tuple[Path, dict[str, Any]]], *, repo_root: Path) -> list[dict[str, Any]]:
    """结果 JSON → 注册建议列表（face/status/evidence_uri/fingerprint/rationale；建议模式）。"""
    set_hash = scenario_set_hash(pairs)
    suggestions: list[dict[str, Any]] = []
    for path, result in pairs:
        scenario = result.get("scenario", "?")
        suite = result.get("suite", "agent-core")
        metrics = result.get("metrics", {})
        suggestions.append(
            {
                "suggestion_type": "orsi_capability_registration",
                "auto_registered": False,  # 建议模式：只建议不注册（--register 为注册模式）
                "proposal": {
                    "name": f"bench.{suite}.{scenario}",
                    "face": _face_for(suite, scenario),
                    "status": "candidate",  # 宪法 3：候选非成品；晋升必经人工审核工单
                    "version": "1.0.0",
                },
                "evidence": {
                    "evidence_uri": str(path.relative_to(repo_root)).replace("\\", "/"),
                    "run_status": result.get("status"),
                    "smoke": result.get("smoke"),
                    "benchmark_ref": result.get("benchmark_ref"),
                    "key_metrics": {
                        k: metrics.get(k)
                        for k in sorted(metrics)
                        if isinstance(metrics.get(k), (int, float, bool))
                    },
                },
                "capability_fingerprint": hashlib.sha256(
                    f"bench-scenarios:{set_hash}:{suite}:{scenario}".encode()
                ).hexdigest(),
                "fingerprint_note": (
                    "基准侧场景集哈希（场景集变=指纹变=能力需重评）；非注册面算法——"
                    "正式注册时由 services/rsi 业务层重算，本值仅供人工比照"
                ),
                "rationale": f"指标实测数据佐证 {scenario} 能力面现状（16 篇 §3 ORSI 参考依据机制）",
            }
        )
    return suggestions


def _rel_uri(path: Path) -> str:
    """仓库内路径 → 相对 URI（正斜杠）；仓库外产物（系统临时目录等）如实记绝对路径。"""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(_REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(resolved).replace("\\", "/")


# ---------------------------------------------------------------- version_diff 装载与劣化判定（17 篇 §3.4）


def read_version_diff(path: Path) -> list[dict[str, Any]]:
    """version_diff 产物 → diff 行列表（17 篇 §2 形状：{metric, prev, curr, delta, trend[, suite]}）。

    兼容三种载体：顶层列表 / {"items": [...]} / {"diffs": [...]}（B 批 diff.py 落地后
    按其实际形状收敛其一，此处宽收不窄拒）。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("items", "diffs"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    raise ValueError(f"version_diff 形状不可解析（期望列表或 {{items|diffs}}）: {path}")


def detect_regressions(
    diff_rows: list[dict[str, Any]], *, threshold: float
) -> list[dict[str, Any]]:
    """劣化判定：trend=↓ 且相对降幅 ≥ threshold 的行（17 篇 §3.4「任一核心指标」逐行判）。

    相对降幅 = |delta| / |prev|（prev=0 无法相对化 → 跳过该行并在返回的
    ``note`` 字段如实说明，不静默不臆判）。
    """
    hits: list[dict[str, Any]] = []
    for row in diff_rows:
        trend = str(row.get("trend", ""))
        if trend not in ("↓", "down"):
            continue
        prev, delta = row.get("prev"), row.get("delta")
        if not isinstance(prev, (int, float)) or not isinstance(delta, (int, float)):
            continue
        if prev == 0:
            hits.append({**row, "regression": False, "note": "prev=0 无法相对化，跳过劣化判定"})
            continue
        rel_drop = abs(delta) / abs(prev)
        if rel_drop >= threshold:
            hits.append({**row, "regression": True, "rel_drop": rel_drop})
    return hits


# ---------------------------------------------------------------- 注册器（v2：--register）


class OrsiRegistrar:
    """eval 结果 → ORSI capability 注册执行体（进程内 ASGI 真调；零 mock 面）。

    进程内 app = FastAPI + 网关同款中间件栈（RequestID/JWT 认证/租户上下文/统一错误体）
    + orsi 路由——与 tests/rsi api_client 形态同源，但依赖零覆盖：JWT 真验签
    （platform Settings.jwt_secret）、PG 会话走 get_session→get_engine 真链路。
    """

    def __init__(
        self,
        *,
        settings: Any,
        link_settings: OrsiLinkSettings,
        tag: str,
        version_diff_uri: str | None = None,
    ) -> None:
        self._settings = settings
        self._link = link_settings
        self._tag = tag
        self._version_diff_uri = version_diff_uri
        self._client: httpx.AsyncClient | None = None

    # ── 进程内装配 ───────────────────────────────────────────────────────
    def _build_app(self) -> Any:
        """最小进程内 app：orsi 路由 + 网关同款中间件（add 逆序=运行序 RequestID→JWT→Tenant→错误体）。"""
        from fastapi import FastAPI

        from services.gateway.middlewares import (
            GlobalExceptionMiddleware,
            JWTAuthMiddleware,
            RequestIDMiddleware,
            TenantContextMiddleware,
        )
        from services.rsi.api.capabilities import router as orsi_router

        app = FastAPI()
        app.state.settings = self._settings  # JWT 验签与 get_session/get_engine 唯一配置源
        app.add_middleware(GlobalExceptionMiddleware)  # 最内层：GatewayError → 四字段错误体
        app.add_middleware(TenantContextMiddleware)
        app.add_middleware(JWTAuthMiddleware)
        app.add_middleware(RequestIDMiddleware)  # 最外层先行：trace_id 注入（审计留痕用）
        app.include_router(orsi_router, prefix="/api/v1")
        return app

    def _auth_headers(self) -> dict[str, str]:
        """签发评测注册 token（rsi:write；claims 经 JWT 真验签——与登录态同链路）。

        scopes 直签 rsi:write 与种子迁移（e3b7d9f1a5c2）后 admin 角色等价——评测自签
        属运营侧凭据，非授权旁路（写面仍过 require_scope 真 PDP 判定）。
        """
        from services.platform.security import build_claims, encode_token

        claims = build_claims(
            user_id=self._link.user_id,
            tenant_id=self._link.tenant_id,
            roles=["admin"],
            scopes=["rsi:write"],
            typ="access",
            ttl_seconds=self._link.token_ttl_seconds,
        )
        return {"Authorization": f"Bearer {encode_token(claims, self._settings.jwt_secret)}"}

    async def __aenter__(self) -> OrsiRegistrar:
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._build_app()), base_url="http://orsi-link"
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ── 注册表面 ─────────────────────────────────────────────────────────
    async def _list_capabilities(self, face: str) -> list[dict[str, Any]]:
        """读面拉取（读公开免 scope，JWT 仍必带）；promotion_evidence 回显供查重/baseline。"""
        assert self._client is not None
        resp = await self._client.get(
            "/api/v1/orsi/capabilities",
            params={"face": face, "page": 1, "page_size": self._link.list_page_size},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return list(resp.json()["data"])

    @staticmethod
    def _find_existing(
        rows: list[dict[str, Any]], *, tag: str, scenario_hash: str, name: str | None = None
    ) -> dict[str, Any] | None:
        """幂等查重：同 tag+scenario_hash 的既有行（name 精确匹配优先——eval 与
        quality_regression 行共享 tag+hash，须按 name 区分 kind）。"""
        for row in rows:
            evidence = row.get("promotion_evidence") or {}
            if evidence.get("eval_tag") != tag or evidence.get("scenario_hash") != scenario_hash:
                continue
            if name is not None and row.get("name") != name:
                continue
            return row
        return None

    @staticmethod
    def _baseline_digest(rows: list[dict[str, Any]], *, tag: str, scenario_hash: str) -> str | None:
        """上一 tag 同场景集的指标摘要（updated_at 最新；17 篇 §3.2 baseline）。

        锚在注册表自身：同 scenario_hash 且 eval_tag 不同的 eval capability 行
        （name=bench.*——排除共享同 hash 的 quality_regression 行）取最新一条的
        metrics_digest——证据链自洽，不依赖文件系统历史；无历史（首轮）→ None。
        """
        candidates = [
            row
            for row in rows
            if str(row.get("name", "")).startswith("bench.")
            and (row.get("promotion_evidence") or {}).get("scenario_hash") == scenario_hash
            and (row.get("promotion_evidence") or {}).get("eval_tag") != tag
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda row: row.get("updated_at") or "")
        return (latest.get("promotion_evidence") or {}).get("metrics_digest")

    async def _register_one(
        self,
        *,
        face: str,
        name: str,
        version: str,
        track: str,
        evidence_uri: str | None,
        scenario_hash: str,
        digest: str,
        baseline_digest: str | None,
    ) -> dict[str, Any]:
        """注册单条（幂等查重先行；409 唯一约束=并发窗口兜底，同样记复用不造假）。"""
        assert self._client is not None
        payload: dict[str, Any] = {
            "face": face,
            "name": name,
            "version": version,
            "source_channel": "L0",  # 评测框架=平台内置产物（06 篇 §1.5 L0 工具面）
            "source_face_track": track,
            "status": "candidate",  # 宪法 3：候选非成品；promote 恒拒红线不动
            "evidence_uri": evidence_uri,
            "promotion_evidence": {
                "eval_tag": self._tag,
                "scenario_hash": scenario_hash,
                "metrics_digest": digest,
                "baseline_digest": baseline_digest,
                "version_diff_uri": self._version_diff_uri,
            },
        }
        resp = await self._client.post("/api/v1/orsi/capabilities", json=payload, headers=self._auth_headers())
        if resp.status_code == 201:
            data = resp.json()["data"]
            return {"id": data["id"], "name": name, "face": face, "reused": False}
        if resp.status_code == 409:  # 查重与注册间并发撞唯一约束：既有行即复用对象
            rows = await self._list_capabilities(face)
            existing = self._find_existing(rows, tag=self._tag, scenario_hash=scenario_hash, name=name)
            if existing is not None:
                return {"id": existing["id"], "name": name, "face": face, "reused": True, "note": "409 复用既有"}
        raise RuntimeError(f"注册失败 HTTP {resp.status_code}: {resp.text[:300]}")

    async def register_eval(self, results_path: Path) -> dict[str, Any]:
        """release-eval 结果 → 注册 eval 证据链 capability（+劣化触发 quality_regression）。"""
        pairs = load_results(results_path)
        if not pairs:
            raise RuntimeError(f"无结果 JSON: {results_path}")
        suites = sorted({str(result.get("suite", "agent-core")) for _, result in pairs})
        scenario_hash = scenario_set_hash(pairs)
        digest = metrics_digest(key_metrics_of(pairs))
        evidence_uri = _rel_uri(results_path)
        if len(suites) != 1:
            # 混套件目录：注册面按套件归属（face 不同），聚合指纹不唯一——显式拒绝不静默混装
            raise RuntimeError(f"结果目录混含多 suite {suites}——请按 suite 目录分次注册（face 归属不同）")
        suite = suites[0]
        face = _SUITE_FACE.get(suite, "O4")  # 17 篇 §3 套件归属裁决（eval 聚合注册不套场景级覆盖）

        rows = await self._list_capabilities(face)
        eval_name = f"bench.{suite}.eval.{scenario_hash[:12]}"
        existing = self._find_existing(rows, tag=self._tag, scenario_hash=scenario_hash, name=eval_name)
        baseline_digest = self._baseline_digest(rows, tag=self._tag, scenario_hash=scenario_hash)

        report: dict[str, Any] = {
            "mode": "register",
            "eval_tag": self._tag,
            "suite": suite,
            "scenario_hash": scenario_hash,
            "metrics_digest": digest,
            "baseline_digest": baseline_digest,
            "version_diff_uri": self._version_diff_uri,
            "registered": [],
            "regressions": [],
        }
        if existing is not None:  # 幂等（17 篇 §3.2 迭代 re-run）：同 tag+hash 复用跳过
            report["registered"].append(
                {"id": existing["id"], "name": existing["name"], "face": face, "reused": True}
            )
        else:
            version = self._tag
            if len(version) > 32:
                raise RuntimeError(f"tag 过长（version 列上限 32）: {version}")
            report["registered"].append(
                await self._register_one(
                    face=face,
                    name=eval_name,
                    version=version,
                    track="normal",
                    evidence_uri=evidence_uri,
                    scenario_hash=scenario_hash,
                    digest=digest,
                    baseline_digest=baseline_digest,
                )
            )

        # 劣化反哺（17 篇 §3.4）：trend=↓ 超阈值 → quality_regression:<metric>（critical 缺口轨）
        if self._version_diff_uri is not None:
            diff_path = Path(self._version_diff_uri)
            if not diff_path.is_absolute():  # 相对路径按仓库根解析（CLI 约定 cwd=仓库根之外的形态）
                diff_path = (_REPO_ROOT / diff_path).resolve()
            if not diff_path.is_file():
                report["regressions_note"] = f"version_diff 文件不存在，跳过劣化检测: {self._version_diff_uri}"
            else:
                report["regressions"] = await self._register_regressions(
                    diff_path, scenario_hash=scenario_hash, digest=digest, baseline_digest=baseline_digest
                )
        else:
            report["regressions_note"] = "未提供 --version-diff，跳过劣化检测（首轮基线常态）"
        return report

    async def _register_regressions(
        self,
        diff_path: Path,
        *,
        scenario_hash: str,
        digest: str,
        baseline_digest: str | None,
    ) -> list[dict[str, Any]]:
        """劣化行 → quality_regression capability 注册（幂等规则同 eval capability）。"""
        hits = [h for h in detect_regressions(read_version_diff(diff_path), threshold=self._link.regression_threshold)
                if h.get("regression")]
        out: list[dict[str, Any]] = []
        for hit in hits:
            metric = str(hit.get("metric", "?"))
            row_suite = hit.get("suite")  # 行内 suite 可选（B 批 diff.py 按同名 suite 运行对比产出）
            # 17 篇 §3.4 face=O7 等；无行内归属 fallback 检索策略
            face = _SUITE_FACE.get(str(row_suite), "O7") if row_suite else "O7"
            reg_name = f"quality_regression:{metric}"
            rows = await self._list_capabilities(face)
            existing = self._find_existing(rows, tag=self._tag, scenario_hash=scenario_hash, name=reg_name)
            if existing is not None:
                out.append(
                    {
                        "metric": metric,
                        "capability_id": existing["id"],
                        "name": existing["name"],
                        "face": face,
                        "rel_drop": hit.get("rel_drop"),
                        "reused": True,
                    }
                )
                continue
            made = await self._register_one(
                face=face,
                name=reg_name,
                version=self._tag,
                track="critical",  # 缺口轨 critical（17 篇 §3.4「评测结果反哺 ORSI 缺口轨」）
                evidence_uri=_rel_uri(diff_path),
                scenario_hash=scenario_hash,
                digest=digest,
                baseline_digest=baseline_digest,
            )
            out.append({"metric": metric, **made, "rel_drop": hit.get("rel_drop")})
        return out


# ---------------------------------------------------------------- CLI


async def _run_register(args: argparse.Namespace, link_settings: OrsiLinkSettings) -> int:
    """注册模式执行（PG 探活失败/注册失败如实退出 1，不静默）。"""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import create_async_engine

    from services.platform.config import Settings

    settings = Settings()
    engine = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM orsi_capabilities LIMIT 1"))
    except (OSError, SQLAlchemyError) as exc:
        print(f"[orsi-link] PG 不可达或 orsi_capabilities 未迁移，注册中止: {exc}", file=sys.stderr)
        await engine.dispose()
        return 1
    await engine.dispose()

    async with OrsiRegistrar(
        settings=settings,
        link_settings=link_settings,
        tag=args.tag,
        version_diff_uri=args.version_diff,
    ) as registrar:
        report = await registrar.register_eval(Path(args.results))
    text_out = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text_out, encoding="utf-8")
    print(text_out)
    made = sum(1 for r in report["registered"] if not r.get("reused"))
    reused = len(report["registered"]) - made
    regressions = sum(1 for r in report["regressions"] if not r.get("reused"))
    print(
        f"[orsi-link] 注册完成: eval_tag={args.tag} 新注册={made} 复用={reused} "
        f"劣化触发={regressions}（阈值={link_settings.regression_threshold:.0%}）",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ORSI capability 注册建议生成（缺省）/ 真实注册（--register）")
    parser.add_argument("--results", required=True, help="结果 JSON 文件或目录（benchmarks/results/<suite>/<date>）")
    parser.add_argument("--out", default=None, help="输出 JSON 路径（缺省打印 stdout）")
    parser.add_argument(
        "--register", action="store_true", help="v2 注册模式：release-eval 后真实调用 /orsi/capabilities（需 PG）"
    )
    parser.add_argument("--tag", default=None, help="注册模式必填：评测 tag（eval_tag/version 列，版本迭代曲线锚点）")
    parser.add_argument(
        "--version-diff", default=None, help="version_diff 产物路径（提供则执行劣化反哺检测；缺省跳过）"
    )
    parser.add_argument(
        "--regression-threshold",
        type=float,
        default=None,
        help="劣化判定阈值（相对降幅；缺省读 BENCH_ORSI_REGRESSION_THRESHOLD，缺省 0.05）",
    )
    args = parser.parse_args(argv)
    results_path = Path(args.results)
    if not results_path.exists():
        print(f"[orsi-link] 结果路径不存在: {results_path}", file=sys.stderr)
        return 1

    if not args.register:
        # 建议模式（v1 兼容）
        pairs = load_results(results_path)
        if not pairs:
            print(f"[orsi-link] 无结果 JSON: {results_path}", file=sys.stderr)
            return 1
        suggestions = build_suggestions(pairs, repo_root=_REPO_ROOT)
        payload = {
            "generated_from": str(results_path),
            "scenarios": sorted(result.get("scenario", "?") for _, result in pairs),
            "suggestions": suggestions,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            print(f"[orsi-link] 建议已写入: {out_path}（{len(suggestions)} 条；未注册——人工确认后走注册端点）")
        else:
            print(text)
        return 0

    # 注册模式（v2）
    if not args.tag or not args.tag.strip():
        print("[orsi-link] --register 须配 --tag（eval_tag 必填）", file=sys.stderr)
        return 2
    overrides: dict[str, Any] = {}
    if args.regression_threshold is not None:
        overrides["regression_threshold"] = args.regression_threshold
    link_settings = OrsiLinkSettings(**overrides)
    if sys.platform == "win32":  # psycopg 异步硬约束（run.py 同款：Selector 循环在 asyncio.run 前固定）
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(_run_register(args, link_settings))
    except (RuntimeError, ValueError) as exc:
        print(f"[orsi-link] 注册失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
