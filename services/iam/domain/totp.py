"""TOTP 领域内核（RFC 6238；api/01 §5.9 totp 三端点 + §5.15 backup-codes 的算法面）。

纯 stdlib 实现（hmac/hashlib/base64/struct），零第三方依赖（最小够用；pyotp 不入依赖树）。
参数定稿：HMAC-SHA1 + 30s 步长 + 6 位数字 + ±1 步时钟漂移容忍（业界默认窗）；
secret=20 字节随机 → base32 无填充（32 字符，RFC 4226 §4 兼容 Google Authenticator）。

安全红线（08 §2.0/§2.2 密钥族）：secret 与备份码明文只在签发响应出现一次；备份码
入库=sha256 hex（32 篇 invite token 同款）；otpauth URI 仅 setup 响应返回不落日志。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

ISSUER = "ontology-agent"
_STEP_SECONDS = 30
_DIGITS = 6
_SKEW_STEPS = 1  # ±1 步（±30s）时钟漂移容忍
_BACKUP_CODE_COUNT = 8  # mock 契约：enable/backup-codes 响应恒 8 枚
_SECRET_BYTES = 20  # 160 bit（RFC 6238 §5.1 推荐下限）


def generate_secret() -> str:
    """base32 无填充 secret（RFC 6238 §5.1；20 字节 → 32 字符）。"""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii").rstrip("=")


def otpauth_uri(secret: str, email: str) -> str:
    """otpauth:// URI（二维码载荷；mock 同构 otpauth://totp/{issuer}:{email}?secret=…&issuer=…，
    label 分隔冒号保留字面、账号段 percent-encode（Keycloak/Authy 同款）。"""
    label = f"{ISSUER}:{quote(email, safe='')}"
    return f"otpauth://totp/{label}?secret={secret}&issuer={ISSUER}"


def _code_at(secret: str, step: int) -> str:
    """单步 HOTP 值（RFC 4226 §5.3：HMAC-SHA1 截断取模）；secret 容错去填充再解码。"""
    key = base64.b32decode(secret.rstrip("=") + "=" * (-len(secret.rstrip("=")) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**_DIGITS)
    return str(value).zfill(_DIGITS)


def verify_code(secret: str, code: str, *, now: float | None = None) -> bool:
    """6 位码校验（±1 步漂移窗；非常量时间比较防时序侧信道）。"""
    normalized = code.strip()
    if len(normalized) != _DIGITS or not normalized.isdigit():
        return False
    current = int(time.time() if now is None else now) // _STEP_SECONDS
    matched = False
    for offset in range(-_SKEW_STEPS, _SKEW_STEPS + 1):
        step = current + offset
        if step < 0:  # 步号非负（计数器语义 RFC 4226 §5.1；now=0 邻域防御）
            continue
        if hmac.compare_digest(normalized, _code_at(secret, step)):
            matched = True  # 全窗扫完再返回（避免提前返回泄漏匹配位置）
    return matched


def generate_backup_codes(count: int = _BACKUP_CODE_COUNT) -> list[str]:
    """一次性备份码（mock 同构 '8432-1097' 四四分段数字；secrets 拒绝可预测源）。"""
    codes: list[str] = []
    seen: set[str] = set()
    while len(codes) < count:
        digits = f"{secrets.randbelow(100_000_000):08d}"
        code = f"{digits[:4]}-{digits[4:]}"
        if code not in seen:  # 撞码重掷（8 位数字空间 1e8，8 枚撞率可忽略仍防御）
            seen.add(code)
            codes.append(code)
    return codes


def backup_code_hash(code: str) -> str:
    """备份码入库哈希（sha256 hex；明文仅在签发响应出现一次）。"""
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()
