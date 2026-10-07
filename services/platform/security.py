"""L2 网关 · 安全原语（密码哈希 / JWT 签发校验 / PDP scope 判定 / 插件两级签名）。

权威设计：
- docs/architecture/08-横切关注点与工程规范.md §2.1（JWT claims）、§2.3（scope 模型）、
  §2.5（PDP 五步）、§2.6（API Key 状态机与 scopes 子集约束）；
- docs/architecture/02-网关层设计.md §3 ③（JWT 认证）、§7（错误码 1xxx）；
- docs/api/01-REST-API契约.md §2.2（Bearer 摘要）；
- docs/Skills/技能与插件设计.md §5.1（两级签名：发布者签 → 平台签，算法 Ed25519）。

M1 子集裁剪（与设计差异均留 TODO）：
- 密码哈希：设计指向 Argon2id，本批按任务裁决用 hashlib.pbkdf2_hmac（零新依赖），
  编码格式与 m1_seed_roles_and_admin 种子迁移一致（pbkdf2:sha256:{iter}$salt_hex$hash_hex），
  升级 Argon2id 时仅扩 verify_password 分支 + 登录重哈希，存储格式不变；
- JWT：HS256（对称密钥=Settings.jwt_secret）；RS256/JWKS 缓存（02 §3 ③）随 M3 多副本改造切换；
- iss/aud 固定模块常量（设计要求可配置，Settings 尚无字段，随 M1 收口补 Settings）。

插件两级签名（M5-1 遗留收口，Skills §5.1 定稿）：
- 算法 Ed25519；发布者对「manifest 摘要 + 包摘要」签（证明包内容出自我手、未被篡改）；
  平台审核通过后对「发布者签名 + 清单」整体再签（认证背书）；
- 验证顺序：先平台签 → 再发布者签，任一失败拒装/拒启（本批落安装时验签；daemon 每次
  启动验签随 plugin-daemon 批次）；
- 密钥：平台私钥来自 config（Settings.platform_plugin_signing_key），缺失即 fail-closed
  （发布 4510 拒绝）；开发期经 OA_PLATFORM_PLUGIN_SIGNING_KEY 提供 dev key（见 config 注释）；
- 依赖：cryptography（2026-09-25 起已在本机运行环境在位；**尚未登记进 pyproject 依赖，
  契约需求随模块报告上报**——禁改 pyproject 约束下的既成事实先用 + 报告补登记）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

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


# ---------------------------------------------------------------- 插件两级签名（Skills §5.1，Ed25519）

PLATFORM_SIG_PREFIX = "platform-ed25519:"
PUBLISHER_SIG_PREFIX = "publisher-ed25519:"
_SIGNING_KEY_BYTES = 32  # ed25519 seed 长度（hex 64 字符）
_SIGNATURE_HEX_LENGTH = 128  # ed25519 签名 64 字节 hex


def _canonical_json(obj: Any) -> bytes:
    """签名域规范化：sort_keys + 紧凑分隔符（同一清单无论键序如何摘要一致）。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def generate_signing_key() -> str:
    """生成 ed25519 私钥（raw seed hex 64 字符；开发期 dev key 供给入口）。"""
    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    return raw.hex()


def public_key_hex_of(private_key_hex: str) -> str:
    """由私钥导出公钥 hex（验签面只需要公钥；安装侧可用独立公钥配置）。"""
    return (
        _parse_private_key(private_key_hex)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex()
    )


def _parse_private_key(private_key_hex: str) -> Ed25519PrivateKey:
    if not isinstance(private_key_hex, str):
        raise ValueError("签名密钥必须为 hex 字符串")
    try:
        raw = bytes.fromhex(private_key_hex)
    except ValueError as exc:
        raise ValueError("签名密钥不是合法 hex") from exc
    if len(raw) != _SIGNING_KEY_BYTES:
        raise ValueError(f"签名密钥长度非法: {len(raw)} 字节（应为 {_SIGNING_KEY_BYTES}）")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _parse_public_key(public_key_hex: str) -> Ed25519PublicKey:
    try:
        raw = bytes.fromhex(public_key_hex)
    except ValueError as exc:
        raise ValueError("验签公钥不是合法 hex") from exc
    if len(raw) != _SIGNING_KEY_BYTES:
        raise ValueError(f"验签公钥长度非法: {len(raw)} 字节（应为 {_SIGNING_KEY_BYTES}）")
    return Ed25519PublicKey.from_public_bytes(raw)


def sign_with_key(private_key_hex: str, payload: bytes) -> str:
    """对字节域签名，返回 hex（底层原语；上层请用语义化 sign_* 函数）。"""
    return _parse_private_key(private_key_hex).sign(payload).hex()


def verify_with_key(public_key_hex: str, payload: bytes, signature_hex: str) -> bool:
    """验签字节域；任何解析/格式异常一律 False（fail-closed，不泄露格式探测面）。"""
    try:
        if len(signature_hex) != _SIGNATURE_HEX_LENGTH:
            return False
        _parse_public_key(public_key_hex).verify(bytes.fromhex(signature_hex), payload)
    except (InvalidSignature, ValueError):
        return False
    return True


def signable_manifest(server_json: Mapping[str, Any]) -> dict[str, Any]:
    """签名域清单：剔除两级签名字段本身（x-platform.signature / publisher_signature）后的深拷贝。"""
    manifest: dict[str, Any] = json.loads(json.dumps(dict(server_json)))  # 清单为纯 JSON 值，深拷贝防共享变异
    x_platform = manifest.get("x-platform")
    if isinstance(x_platform, dict):
        x_platform.pop("signature", None)
        x_platform.pop("publisher_signature", None)
    return manifest


def publisher_payload(server_json: Mapping[str, Any], checksum: str) -> bytes:
    """发布者签名域 = manifest 摘要 + 包摘要（Skills §5.1 第一级）。"""
    return _canonical_json({"manifest": signable_manifest(server_json), "checksum": checksum})


def platform_payload(server_json: Mapping[str, Any], checksum: str, publisher_signature: str | None) -> bytes:
    """平台签名域 = 发布者签名 + 清单（+ 包摘要绑定；Skills §5.1 第二级）。"""
    return _canonical_json(
        {
            "manifest": signable_manifest(server_json),
            "checksum": checksum,
            "publisher_signature": publisher_signature or "",
        }
    )


def format_signature(prefix: str, signature_hex: str) -> str:
    return f"{prefix}{signature_hex}"


def parse_signature(value: str | None, prefix: str) -> str | None:
    """严格解析签名串：前缀逐字匹配 + 128 位 hex；不符返回 None（占位签/残签据此拒验）。"""
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    signature_hex = value[len(prefix) :]
    if len(signature_hex) != _SIGNATURE_HEX_LENGTH:
        return None
    try:
        bytes.fromhex(signature_hex)
    except ValueError:
        return None
    return signature_hex


def sign_publisher_plugin(publisher_private_key_hex: str, *, server_json: Mapping[str, Any], checksum: str) -> str:
    """开发者签（两级第一级）：对 manifest+包摘要签名，串行化进 x-platform.publisher_signature。"""
    return format_signature(
        PUBLISHER_SIG_PREFIX, sign_with_key(publisher_private_key_hex, publisher_payload(server_json, checksum))
    )


def verify_publisher_plugin(
    publisher_public_key_hex: str | None,
    *,
    server_json: Mapping[str, Any],
    checksum: str,
    publisher_signature: str | None,
) -> bool:
    """开发者签验签位：公钥/签名任一缺失即 False（开发者签缺失拒——fail-closed）。"""
    signature_hex = parse_signature(publisher_signature, PUBLISHER_SIG_PREFIX)
    if not publisher_public_key_hex or signature_hex is None:
        return False
    return verify_with_key(publisher_public_key_hex, publisher_payload(server_json, checksum), signature_hex)


def verify_platform_plugin(
    platform_public_key_hex: str,
    *,
    server_json: Mapping[str, Any],
    checksum: str,
    publisher_signature: str | None,
    platform_signature: str | None,
) -> bool:
    """平台签验签：伪造签/清单篡改/占位旧签一律 False（Skills §5.1 验证顺序第一级）。"""
    signature_hex = parse_signature(platform_signature, PLATFORM_SIG_PREFIX)
    if signature_hex is None:
        return False
    return verify_with_key(
        platform_public_key_hex, platform_payload(server_json, checksum, publisher_signature), signature_hex
    )


class PluginSigner:
    """平台签名器（发布联动真签 + 安装时验签共用；密钥来自 config，缺失 fail-closed）。

    fail-closed 口径：密钥未配置（``ready=False``）时 sign/verify 一律拒绝——发布用例映射
    4510 拒绝发布；安装用例同样拒绝（不静默降级，Sandbox §3.2 同款纪律）。
    """

    def __init__(self, private_key_hex: str | None) -> None:
        if private_key_hex is None:
            self._private_hex: str | None = None
        else:
            _parse_private_key(private_key_hex)  # 构造期即验形（配置错误启动即暴露）
            self._private_hex = private_key_hex.lower()

    @property
    def ready(self) -> bool:
        return self._private_hex is not None

    @property
    def public_key_hex(self) -> str:
        if self._private_hex is None:
            raise ValueError("平台签名密钥未配置（fail-closed）")
        return public_key_hex_of(self._private_hex)

    def sign_platform(self, *, server_json: Mapping[str, Any], checksum: str, publisher_signature: str | None) -> str:
        """平台签（发布联动：审核通过后对「发布者签名 + 清单」整体再签——认证背书）。"""
        if self._private_hex is None:
            raise ValueError("平台签名密钥未配置，发布拒绝（fail-closed）")
        payload = platform_payload(server_json, checksum, publisher_signature)
        return format_signature(PLATFORM_SIG_PREFIX, sign_with_key(self._private_hex, payload))

    def verify_platform(
        self,
        *,
        server_json: Mapping[str, Any],
        checksum: str,
        publisher_signature: str | None,
        platform_signature: str | None,
    ) -> bool:
        """平台签验签（密钥缺失抛 ValueError，由调用方映射 fail-closed 错误码）。"""
        if self._private_hex is None:
            raise ValueError("平台签名密钥未配置，验签拒绝（fail-closed）")
        return verify_platform_plugin(
            self.public_key_hex,
            server_json=server_json,
            checksum=checksum,
            publisher_signature=publisher_signature,
            platform_signature=platform_signature,
        )
