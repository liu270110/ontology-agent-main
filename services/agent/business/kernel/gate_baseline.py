"""B1 门禁基线（02 §2 B1：平台所有的前置基线校验，硬编码内核、包 gate 只增不替）。

基线四规则（行动类枚举合法性 / 参数域 / scope / executionMode 分级审批）确定性求值
（推理分级宪法：高频逻辑走规则，禁 LLM）；错误码一律取 platform/errors.ErrorCode 登记段。
合成顺序固定「基线先、包后」（§4 表 gates 行）：包 gate 只能附违例，基线拒绝不被覆盖；
包 gate 自称基线（GateReport.is_baseline=True）视为篡改基线权威，整步拒绝（铁律 1 负向测试）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from services.agent.business.kernel.errors import KernelContractError
from services.agent.domain.model.kernel_actions import (
    ActionDecision,
    ApprovalTicket,
    ExecutionMode,
)
from services.agent.domain.model.kernel_context import TenantContext
from services.agent.domain.model.kernel_gates import GateFinding, GateReport, GateVerdict
from services.agent.domain.model.kernel_planning import PlanStep
from services.platform.errors import ErrorCode

_BASELINE_REPORTER = "kernel.baseline"
_APPROVAL_REQUIRED_MODES = frozenset({ExecutionMode.EXTERNAL_WRITE, ExecutionMode.CODE})


def canonical_param_hash(parameters: dict[str, Any]) -> str:
    """参数哈希（B5 参数哈希绑定键）：canonical JSON（排序键）sha256，值不经采样。"""
    canonical = json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finding(focus: str, rule: str, code: ErrorCode, message: str) -> GateFinding:
    return GateFinding(focus=focus, rule_iri=rule, severity="error", code=int(code), message=message)


def _check_param_domain(decision: ActionDecision, schema: dict[str, Any]) -> list[GateFinding]:
    """参数域校验（JSON Schema 子集：required/类型/enum/禁未知键）——形状校验，确定性。"""
    findings: list[GateFinding] = []
    rule = "kernel.baseline.param-domain"
    for name in schema.get("required", []):
        if not isinstance(name, str) or name not in decision.parameters:
            findings.append(_finding(decision.action_iri, rule, ErrorCode.PARAM_INVALID, f"缺少必填参数: {name}"))
    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        type_map: dict[str, tuple[type, ...]] = {
            "string": (str,),
            "number": (int, float),
            "integer": (int,),
            "boolean": (bool,),
            "object": (dict,),
            "array": (list,),
        }
        for key, value in decision.parameters.items():
            spec = properties.get(key)
            if spec is None:
                if schema.get("additionalProperties") is False:
                    findings.append(
                        _finding(
                            f"{decision.action_iri}#{key}",
                            rule,
                            ErrorCode.PARAM_INVALID,
                            f"未知参数: {key}",
                        )
                    )
                continue
            expected = spec.get("type") if isinstance(spec, dict) else None
            allowed = type_map.get(str(expected)) if expected else None
            if allowed and not isinstance(value, allowed):
                findings.append(
                    _finding(
                        f"{decision.action_iri}#{key}",
                        rule,
                        ErrorCode.PARAM_INVALID,
                        f"参数 {key} 类型须为 {expected}",
                    )
                )
                continue
            enum_values = spec.get("enum") if isinstance(spec, dict) else None
            if isinstance(enum_values, list) and value not in enum_values:
                findings.append(
                    _finding(
                        f"{decision.action_iri}#{key}",
                        rule,
                        ErrorCode.PARAM_INVALID,
                        f"参数 {key} 不在枚举域 {enum_values}",
                    )
                )
    return findings


class BaselineGate:
    """内核基线门禁（B1）：单例可复用，纯确定性；任何通道不可替换（07 §7.4 铁律 1）。"""

    def check(
        self,
        decision: ActionDecision,
        step: PlanStep,
        ctx: TenantContext,
        *,
        tool_bound: bool,
        approval: ApprovalTicket | None,
        param_hash: str,
    ) -> GateReport:
        """基线求值：四规则全过 ALLOW；任一 error 级违例 REJECT（含理由回流）。"""
        findings: list[GateFinding] = []
        # R1 行动类枚举合法性：tools.bindings 无绑定即非法行动类
        if not tool_bound:
            findings.append(
                _finding(
                    decision.action_iri,
                    "kernel.baseline.action-enum",
                    ErrorCode.PARAM_INVALID,
                    f"行动类未注册工具绑定: {decision.action_iri}",
                )
            )
        # R2 参数域
        findings.extend(_check_param_domain(decision, step.parameter_schema))
        # R3 scope 校验（授权唯一依据=计划步 required_scopes ⊆ 租户 scopes）
        missing = sorted(set(step.required_scopes) - set(ctx.scopes))
        if missing:
            findings.append(
                _finding(
                    decision.action_iri,
                    "kernel.baseline.scope",
                    ErrorCode.SCOPE_INSUFFICIENT,
                    f"scope 不足: 缺 {missing}",
                )
            )
        # R4 executionMode 分级审批（B5 联动）：缺回执不在此拒——路由到执行阶段 waiting_approval
        # 支走「超时默认拒绝」（04 §3 状态机）；回执存在但哈希不匹配 = 换参重放，门禁即拒。
        if decision.execution_mode in _APPROVAL_REQUIRED_MODES and approval is not None:
            if approval.param_hash != param_hash:
                findings.append(
                    _finding(
                        decision.action_iri,
                        "kernel.baseline.approval-hash",
                        ErrorCode.SCOPE_INSUFFICIENT,
                        "审批回执参数哈希与当前调用不一致（防换参重放）",
                    )
                )
        verdict = GateVerdict.REJECT if findings else GateVerdict.ALLOW
        return GateReport(verdict=verdict, is_baseline=True, findings=tuple(findings))

    @staticmethod
    def compose(baseline: GateReport, pack_reports: Sequence[GateReport]) -> GateReport:
        """合成（基线先、包后，只增不替）：基线拒绝不可被覆盖放行；包 gate 只能以「新增违例」
        把放行翻成拒绝（error 级 findings 计入终局）；包 gate 自称基线即篡改。"""
        merged: list[GateFinding] = list(baseline.findings)
        pack_violation = False
        for report in pack_reports:
            if report.is_baseline:
                raise KernelContractError(f"包 gate {report.reporter} 自称基线报告，篡改基线权威（铁律 1 只增不替）")
            merged.extend(report.findings)
            pack_violation = pack_violation or any(f.severity == "error" for f in report.findings)
        final = GateVerdict.REJECT if (baseline.verdict is GateVerdict.REJECT or pack_violation) else GateVerdict.ALLOW
        return GateReport(
            verdict=final,
            is_baseline=True,
            findings=tuple(merged),
            reporter=_BASELINE_REPORTER,
        )
