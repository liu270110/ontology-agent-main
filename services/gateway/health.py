"""L2 网关 · 聚合就绪探活（readyz；app.py TODO(M3) 销项，02 篇 §2/§4 探针面）。

- GET /readyz（挂 /api/v1 前缀；根路径 /readyz 兼容别名在 app.py）：聚合 PG（SELECT 1）/
  Redis（ping）/ MinIO（bucket exists，可选——SDK 未进 pyproject 依赖即 skipped 不降级）；
- 任一硬依赖失败 → 503 + 依赖明细（checks.{name}.{ok,error,latency_ms,skipped}）；
  healthz 保持轻量存活口径（app.py 内联，不触外部依赖）。

装配纪律：引擎与 Redis 一律经 ``platform.deps`` 进程内单例（lru_cache，lifespan 预热），
**禁每请求新建连接池**（M3 首次调用经 deps 单例解析后挂 ``app.state``（sentinel 防重复解析），
后续请求直接复用同一实例——即 lifespan 预热的那一个，不随请求重建）；
单依赖探活超时 2s，防挂死依赖拖垮探针；
探活异常一律转依赖明细（503 语义），禁裸 500。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from services.platform.deps import get_engine, get_redis

if TYPE_CHECKING:  # 仅类型注解（运行时零 import——分层纪律同 deps.py）
    from services.platform.config import Settings

router = APIRouter(tags=["probe"])

_PROBE_TIMEOUT_S = 2.0  # 单依赖探活超时（挂死依赖不拖垮 readyz）
_MINIO_ARTIFACT_BUCKET = "oa-artifacts"  # 制品桶（TBox/沙箱快照 MinIO 化统一桶名，随 SDK 装配生效）

#: 探活函数统一返回 (ok, skipped, error)；skipped=True 为可选依赖未装配，不计失败
_CheckResult = tuple[bool, bool, str | None]

#: 「未解析」哨兵：区分 app.state 上的占位 None（如 MinIO SDK 缺省装配结果）与从未解析
_MISSING = object()


def _probe_singleton(request: Request, attr: str, factory: Callable[[Settings], Any]) -> Any:
    """app 级单例解析：首次经 factory 装配并挂 ``app.state.<attr>``，其后直接复用。

    M3 验收约束「禁每请求新建连接池」：生产路径 factory=deps.get_engine/get_redis（lru_cache
    进程内单例，即 lifespan 预热的那一个）；本 helper 保证即使工厂无缓存（测试替身）也只构造一次。
    """
    instance = getattr(request.app.state, attr, _MISSING)
    if instance is _MISSING:
        instance = factory(request.app.state.settings)
        setattr(request.app.state, attr, instance)
    return instance


class DependencyCheckOut(BaseModel):
    """单依赖探活明细（extra=forbid，02 篇 §6 DTO 铁律）。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    latency_ms: int
    error: str | None = None
    skipped: bool = False


class ReadyzOut(BaseModel):
    """readyz 聚合输出：status=ok|degraded + 各依赖明细（503 时逐项可读）。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    version: str
    profile: str
    checks: dict[str, DependencyCheckOut]


def _load_minio(settings: Settings) -> Any | None:
    """可选 MinIO SDK 装配（pyproject 暂无 minio 依赖 → None=skipped；依赖进册后自动启用）。"""
    try:
        from minio import Minio  # 可选依赖，缺省路径返回 None（禁阻塞启动）
    except ImportError:
        return None
    return Minio(
        settings.minio_endpoint, access_key=settings.minio_user, secret_key=settings.minio_password, secure=False
    )


async def _check_postgres(request: Request) -> _CheckResult:
    """PG 探活：复用进程内引擎单例（禁每请求新建连接池），SELECT 1 走池内连接。"""
    engine = _probe_singleton(request, "readyz_pg_engine", get_engine)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True, False, None


async def _check_redis(request: Request) -> _CheckResult:
    """Redis 探活：复用进程内客户端单例，ping 即往返。"""
    await _probe_singleton(request, "readyz_redis_client", get_redis).ping()
    return True, False, None


async def _check_minio(request: Request) -> _CheckResult:
    """MinIO bucket 探活（可选）：SDK 未装配 → skipped=True 不降级（同步 SDK 入线程池）。"""
    client = _probe_singleton(request, "readyz_minio_client", _load_minio)
    if client is None:
        return True, True, None
    exists = await asyncio.to_thread(client.bucket_exists, _MINIO_ARTIFACT_BUCKET)
    if not exists:
        return False, False, f"bucket 不存在: {_MINIO_ARTIFACT_BUCKET}（初始化任务随部署批次）"
    return True, False, None


async def _measure(probe: Coroutine[Any, Any, _CheckResult]) -> DependencyCheckOut:
    """单依赖探活计时包装：超时/异常一律转依赖明细（503 语义），禁裸 500。"""
    started = time.perf_counter()
    try:
        ok, skipped, error = await asyncio.wait_for(probe, timeout=_PROBE_TIMEOUT_S)
    except TimeoutError:
        ok, skipped, error = False, False, f"timeout>{_PROBE_TIMEOUT_S}s"
    except Exception as exc:  # noqa: BLE001 ——探活吃掉一切异常转明细（依赖挂≠网关崩）
        ok, skipped, error = False, False, f"{type(exc).__name__}: {exc}"
    return DependencyCheckOut(
        ok=ok, latency_ms=int((time.perf_counter() - started) * 1000), error=error, skipped=skipped
    )


@router.get("/readyz", summary="聚合就绪探活（PG/Redis/MinIO；任一硬依赖失败 503+明细）")
async def readyz(request: Request) -> JSONResponse:
    """聚合三依赖探活：deploy compose healthcheck / 编排就绪门对应；healthz 保持轻量存活。"""
    settings: Settings = request.app.state.settings
    checks = {
        "postgres": await _measure(_check_postgres(request)),
        "redis": await _measure(_check_redis(request)),
        "minio": await _measure(_check_minio(request)),
    }
    hard_failed = [name for name, check in checks.items() if not check.skipped and not check.ok]
    body = ReadyzOut(
        status="degraded" if hard_failed else "ok",
        version=request.app.version,
        profile=settings.deploy_profile,
        checks=checks,
    ).model_dump()
    return JSONResponse(body, status_code=503 if hard_failed else 200)
