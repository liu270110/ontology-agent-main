"""电力工单系统 REST 连接器（真实部署形态骨架；业务回写设计 §5 连接器纪律 + §6 电力场景）。

与 Mock 连接器（``mock_power_ticket.py``）同一 ``BizSystemAdapter`` 契约，差异只在传输层：
进程内调用 → REST。连接器保持**无状态薄层**（§5.1）：只做鉴权（Bearer token）、参数映射
（契约对象 ↔ HTTP 报文）、幂等键透传（``Idempotency-Key`` 头）；重试/对账/台账等可靠性逻辑
一律在 action_dispatcher（retryable_codes 裁决），本模块内不得重复实现。

端点映射（部署形态约定；鉴权头全端点携带，幂等键全写操作携带）：

==================  ============================================  ==============================
契约方法            HTTP                                          幂等键承载
==================  ============================================  ==============================
execute             POST {base}/orders                            头 Idempotency-Key（原键）
query_status        GET {base}/orders/by-key/{key}                路径段（键字符集 UUID+冒号，路径安全）
compensate          POST {base}/orders/{receipt_no}/cancel        头 Idempotency-Key（补偿键）
check_health        GET {base}/health                             —
==================  ============================================  ==============================

响应/异常 → 契约错误码映射（§5.3 接入清单第 2/3/4 项）：

- 2xx → 受理凭证（回执体含 ``receipt_no``/``occurred_at``）；
- 4xx → ``VALIDATION_ERROR``（脏数据语义，不可重试）；
- execute 遇 409/422 → 同键重复单，按幂等语义**转查单**回执（§2.2 同键已受理返回首次结果）；
- 429 → ``RATE_LIMITED``（可重试）；5xx → ``TEMP_UNAVAILABLE``（临时不可用，可重试；
  与 WritebackPolicy.retryable_codes 同词汇，Mock 故障注入同码）；
- compensate 遇 409/422 → 终态不可撤，业务失败码透传（如 ``ORDER_TERMINAL``）；
- ``httpx.TimeoutException`` → ``TimeoutError``（dispatcher wait_for 分支裁决=unknown §2.5）：
  是否重试/转对账由 dispatcher 裁决（§2.5）。

零真网：``client`` 可注入 ``httpx.AsyncClient(transport=httpx.MockTransport(...))`` 供测试
脚本化；生产由组合根注入持有连接池的真客户端。
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from services.writeback.adapters.base import (
    AdapterError,
    BizStatusResult,
    CompensationRequest,
    ConnectorMeta,
    HealthReport,
    WritebackReceipt,
    WritebackRequest,
)
from services.writeback.adapters.mock_power_ticket import (
    ACTION_IRI_CANCEL_ORDER,
    ACTION_IRI_CREATE_ORDER,
    ORDER_CANCELLED,
    ORDER_CREATED,
    ORDER_DISPATCHED,
    ORDER_DONE,
)

SYSTEM_NAME = "http-power-ticket"

# 业务侧原生态 → (finished, success) 归一化三值语义（状态机常量复用 Mock，§6.1 created→dispatched→done/cancelled）
_STATUS_SEMANTICS: dict[str, tuple[bool | None, bool | None]] = {
    ORDER_DONE: (True, True),  # 终态：成功
    ORDER_CANCELLED: (True, False),  # 终态：失败
    ORDER_CREATED: (False, None),  # 在途：不能定性
    ORDER_DISPATCHED: (False, None),  # 在途：不能定性
}


class HttpPowerTicketAdapter:
    """电力工单 REST 连接器（create_order / query_order / cancel_order 端点集，§6.1 同接口集）。

    ``client`` 注入即接管生命周期（测试用 MockTransport 客户端）；缺省自建客户端并随
    ``aclose`` 释放（组合根 shutdown 位）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._timeout = timeout  # 注入客户端同样生效（per-request 超时，组合根预算不旁路）
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def aclose(self) -> None:
        """释放自建客户端（注入客户端的生命周期归注入方，不代管）。"""
        if self._owns_client:
            await self._client.aclose()

    # ---- BizSystemAdapter 契约（§5.1）----

    async def check_health(self) -> HealthReport:
        """连通性/鉴权预检：只报布尔与详情，不抛错（探测面语义）。"""
        try:
            resp = await self._client.get(f"{self._base_url}/health", headers=self._headers(), timeout=self._timeout)
        except httpx.HTTPError as exc:
            return HealthReport(ok=False, detail={"system": SYSTEM_NAME, "error": str(exc)})
        return HealthReport(ok=resp.is_success, detail={"system": SYSTEM_NAME, "status_code": resp.status_code})

    async def execute(self, req: WritebackRequest) -> WritebackReceipt:
        """创建工单（幂等键透传 ``Idempotency-Key`` 头；受理即回凭证 §2.3）。"""
        payload = {
            "action_iri": req.action_iri,
            "params": dict(req.params),
            "tenant_id": str(req.tenant_id),
            "action_instance_id": str(req.action_instance_id),
            "trace_id": req.trace_id,
        }
        try:
            resp = await self._client.post(
                f"{self._base_url}/orders",
                json=payload,
                headers=self._headers(req.idempotency_key),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise self._timeout_error("create_order") from exc
        except httpx.TransportError as exc:  # 连接失败（未达业务侧）：网络错误语义，可重试性归 dispatcher
            raise AdapterError("NETWORK_ERROR", f"create_order 连接失败: {exc}") from exc
        if resp.status_code in (409, 422):
            return await self._receipt_by_key(req.idempotency_key)  # 同键重复单 → 转查单（§2.2 幂等语义）
        self._raise_for_status(resp, context="create_order")
        body = self._json(resp)
        receipt_no = str(body.get("receipt_no") or "")
        if not receipt_no:  # §2.3 红线：无凭证的「成功」一律视为 unknown（accepted=False → dispatcher 转未知态）
            return WritebackReceipt(
                accepted=False, receipt_no="", idempotency_key=req.idempotency_key, occurred_at="", raw=body
            )
        return self._receipt(receipt_no, req.idempotency_key, body)

    async def query_status(self, idempotency_key: str) -> BizStatusResult:
        """按幂等键查工单真实状态（unknown 核实 §2.5 与对账 §4 依赖）。"""
        try:
            resp = await self._client.get(
                f"{self._base_url}/orders/by-key/{idempotency_key}",
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise self._timeout_error("query_order") from exc
        except httpx.TransportError as exc:
            raise AdapterError("NETWORK_ERROR", f"query_order 连接失败: {exc}") from exc
        if resp.status_code == 404:
            return BizStatusResult(status="unknown", finished=None, success=None, receipt_no=None)
        self._raise_for_status(resp, context="query_order")
        body = self._json(resp)
        status = str(body.get("status") or "")
        finished, success = _STATUS_SEMANTICS.get(status, (None, None))  # 未登记原生态=业务侧不可判定
        return BizStatusResult(
            status=status or "unknown", finished=finished, success=success, receipt_no=body.get("receipt_no"), raw=body
        )

    async def compensate(self, req: CompensationRequest) -> WritebackReceipt:
        """撤销工单（补偿走同一契约 §3.2：幂等键=补偿键、凭证、原单以 receipt_no 定位）。"""
        receipt_no = str(req.original_receipt.get("receipt_no") or "")
        if not receipt_no:
            raise AdapterError("ORDER_NOT_FOUND", "原单回执缺失 receipt_no（无法定位原单，§3.2 补偿事实依据）")
        try:
            resp = await self._client.post(
                f"{self._base_url}/orders/{receipt_no}/cancel",
                json={"reason": req.reason},
                headers=self._headers(req.idempotency_key),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise self._timeout_error("cancel_order") from exc
        except httpx.TransportError as exc:
            raise AdapterError("NETWORK_ERROR", f"cancel_order 连接失败: {exc}") from exc
        if resp.status_code == 404:
            raise AdapterError("ORDER_NOT_FOUND", f"cancel_order 原单不存在: {receipt_no}")
        if resp.status_code in (409, 422):  # 终态不可撤（§6.1 cancel_order 语义）：业务失败码透传
            body = self._json(resp)
            raise AdapterError(
                str(body.get("error") or "ORDER_TERMINAL"),
                str(body.get("message") or f"cancel_order 原单不可撤: {receipt_no}"),
            )
        self._raise_for_status(resp, context="cancel_order")
        return self._receipt(receipt_no, req.idempotency_key, self._json(resp))

    # ---- 内部：报文/错误映射（无状态薄层，无重试无台账）----

    async def _receipt_by_key(self, idempotency_key: str) -> WritebackReceipt:
        """重复单（409/422）→ 按幂等语义转查单，以业务侧首次受理结果作回执（§2.2）。"""
        result = await self.query_status(idempotency_key)
        if not result.receipt_no:  # 空串/缺失/非字符串同视为未命中（同 execute 的 §2.3 红线口径）
            raise AdapterError("ORDER_NOT_FOUND", f"重复单回查未命中（键 {idempotency_key}）")
        return self._receipt(result.receipt_no, idempotency_key, dict(result.raw))

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_token}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key  # 幂等键透传（§5.1 连接器纪律）
        return headers

    def _receipt(self, receipt_no: str, idempotency_key: str, body: dict[str, Any]) -> WritebackReceipt:
        return WritebackReceipt(
            accepted=True,
            receipt_no=receipt_no,
            idempotency_key=idempotency_key,  # 键回显（§2.3 凭证三要素：受理号+时间戳+键回显）
            occurred_at=str(body.get("occurred_at") or ""),
            raw=body,  # 业务系统原样回执（审计事实依据）
        )

    @staticmethod
    def _timeout_error(context: str) -> TimeoutError:
        """超时 = 受理未知态（§2.5）：抛 TimeoutError 交 dispatcher wait_for 分支裁决 unknown（禁盲目重试）。"""
        return TimeoutError(f"{context} 业务侧响应超时（受理未知态 unknown，§2.5）")

    @staticmethod
    def _raise_for_status(resp: httpx.Response, *, context: str) -> None:
        """非 2xx → 契约错误码映射（§5.3：4xx 脏数据不可重试；429 限流/5xx 临时不可用可重试）。"""
        if resp.is_success:
            return
        if resp.status_code == 429:
            code = "RATE_LIMITED"
        elif resp.status_code >= 500:
            code = "TEMP_UNAVAILABLE"
        else:
            code = "VALIDATION_ERROR"  # 其余 4xx（含 3xx 未跟随）：脏数据语义=不可重试
        raise AdapterError(code, f"{context} 业务侧响应 {resp.status_code}: {resp.text[:200]}")

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError:  # 非 JSON 回执体：按空对象处理，原文留在 raw 外（错误消息已截断携带）
            return {}
        return body if isinstance(body, dict) else {}


def http_power_ticket_meta(
    connector_id: uuid.UUID | None = None,
    *,
    action_iris: frozenset[str] | None = None,
    risk_level: str = "medium",
) -> ConnectorMeta:
    """连接器注册元数据（§5.2 四项声明；绑定 IRI 与 Mock 同源两行动类）。"""
    return ConnectorMeta(
        connector_id=connector_id or uuid.uuid4(),
        name=SYSTEM_NAME,
        action_iris=action_iris or frozenset({ACTION_IRI_CREATE_ORDER, ACTION_IRI_CANCEL_ORDER}),
        risk_level=risk_level,
        supports_query_status=True,
        supports_compensate=True,
        idempotency_mode="native",  # 业务侧按 Idempotency-Key 头去重（§5.3 第 3 项）
    )
