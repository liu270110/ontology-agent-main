"""L2 网关 · 安全原语（密码哈希 / JWT 签发校验 / PDP scope 判定）。

权威设计：
- docs/architecture/08-横切关注点与工程规范.md §2.1（JWT claims）、§2.3（scope 模型）、
  §2.5（PDP 五步）、§2.6（API Key 状态机与 scopes 子集约束）；
- docs/architecture/02-网关层设计.md §3 ③（JWT 认证）、§7（错误码 1xxx）；
- docs/api/01-REST-API契约.md §2.2（Bearer 摘要）。

M1 子集裁剪（与设计差异均留 TODO）：
- 密码哈希：设计指向 Argon2id，本批按任务裁决用 hashlib.pbkdf2_hmac（零新依赖），
  编码格式与 m1_seed_roles_and_admin 种子迁移一致（pbkdf2:sha256:{iter}$salt_hex$hash_hex），
  升级 Argon2id 时仅扩 verify_password 分支 + 登录重哈希，存储格式不变；
- JWT：HS256（对称密钥=Settings.jwt_secret）；RS256/JWKS 缓存（02 §3 ③）随 M3 多副本改造切换；
- iss/aud 固定模块常量（设计要求可配置，Settings 尚无字段，随 M1 收口补 Settings）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import jwt

# ---------------------------------------------------------------- 密码哈希（08 §2.0 补充①）

_PBKDF2_ALGORITHM = "sha256"
_PBKDF2_ITERATIONS = 600_000  # OWASP 2023 推荐（sha256 ≥ 60 万轮）；与种子迁移一致
_SALT_BYTES = 16


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """pbkdf2_sha256 哈希，格式 pbkdf2:sha256:{iter}$salt_hex$hash_hex（与种子迁移兼容）。"""
    if not password:
        raise ValueError("password 不能为空")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(_PBKDF2_ALGORITHM, password.encode("utf-8"), salt, iterations)
    return f"pbkdf2:{_PBKDF2_ALGORITHM}:{iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码；解析失败一律 False（不抛异常，防格式探测侧信道）。"""
    try:
        scheme, algorithm, iterations, salt_hex, digest_hex = password_hash.replace(":", "$").split("$")
        if scheme != "pbkdf2" or algorithm != _PBKDF2_ALGORITHM:
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            _PBKDF2_ALGORITHM, password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------- JWT（08 §2.1 claims）

ISSUER = "ontology-agent"
AUDIENCE = "ontology-agent-api"
ALGORITHM = "HS256"

# 08 §2.1 claims 全集；scope 命名 08 §2.3（resource:action，精确匹配）
# 验收修复：exp 入必查集（无 exp 的令牌拒绝，防永不过期）
REQUIRED_CLAIMS = ("sub", "tenant_id", "roles", "scopes", "typ", "jti", "exp")


class TokenError(Exception):
    """JWT 校验失败基类（调用方映射 1002/1003）。"""

    def __init__(self, message: str, *, expired: bool = False) -> None:
        super().__init__(message)
        self.expired = expired


def build_claims(
    *,
    user_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str,
    roles: list[str],
    scopes: list[str],
    typ: str,
    ttl_seconds: int,
) -> dict[str, Any]:
    """构造 08 §2.1 claims。typ ∈ {access, refresh}（08 §2.0 通道表）。"""
    if typ not in ("access", "refresh"):
        raise ValueError(f"非法 typ: {typ}")
    now = int(datetime.now(UTC).timestamp())
    return {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "roles": list(roles),
        "scopes": list(scopes),
        "typ": typ,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + ttl_seconds,
    }


def encode_token(claims: Mapping[str, Any], secret: str) -> str:
    """签发 JWT（HS256；02 §3 ③ RS256 随 M3 切换）。iss/aud 为 payload claim（decode 侧校验）。"""
    payload = dict(claims)
    payload.setdefault("iss", ISSUER)
    payload.setdefault("aud", AUDIENCE)
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(token: str, secret: str, *, expected_typ: str | None = None) -> dict[str, Any]:
    """验签 + exp/iss/aud 校验 + 必备 claims 检查；typ 不符按无效处理（08 §2.1）。"""
    try:
        claims: dict[str, Any] = jwt.decode(token, secret, algorithms=[ALGORITHM], audience=AUDIENCE, issuer=ISSUER)
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("令牌已过期", expired=True) from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("令牌验签/iss/aud 不通过") from exc
    missing = [c for c in REQUIRED_CLAIMS if c not in claims]
    if missing:
        raise TokenError(f"令牌缺失 claims: {','.join(missing)}")
    if expected_typ is not None and claims["typ"] != expected_typ:
        raise TokenError("令牌类型不符")
    return claims


# ---------------------------------------------------------------- PDP scope 判定（08 §2.5）


def authorize(subject_scopes: list[str] | tuple[str, ...], required_scope: str) -> bool:
    """PDP 第 3 步（动作判定）：scope 精确匹配，deny-by-default（08 §2.3/§2.5）。

    五步定位：第 1 步（主体有效性）由 JWTAuthMiddleware/登录完成；第 2 步（租户归属谓词）
    由仓储层强制 tenant_id 过滤；第 4 步（工具 ACL/行级谓词）归 L3 调用点；第 5 步（留痕）
    由调用方落审计——本函数只做单一动作判定，REST 与 MCP 出口共用（禁各路由另写一套）。
    """
    if not required_scope:
        raise ValueError("required_scope 不能为空")
    return required_scope in set(subject_scopes)


def api_key_scopes_valid(owner_scopes: list[str] | tuple[str, ...], key_scopes: list[str] | tuple[str, ...]) -> bool:
    """08 §2.3 红线：API Key 的 scopes 必须是其 owner 用户 scopes 的子集（签发/轮换时校验）。"""
    owner = set(owner_scopes)
    return all(scope in owner for scope in key_scopes)


def remaining_ttl_seconds(claims: Mapping[str, Any], *, now: float | None = None) -> int:
    """jti 黑名单 TTL=剩余有效期（08 §2.1 / api/01 §2.2）。"""
    current = int(time.time() if now is None else now)
    return max(int(claims.get("exp", current)) - current, 0)
