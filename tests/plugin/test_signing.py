"""平台两级签名测试（Skills §5.1 Ed25519；进程内纯函数，无 PG 依赖）。

覆盖：真签过（开发者签 → 平台签 → 双验）、伪造平台签拒、清单篡改拒、开发者签缺失拒、
密钥缺失 fail-closed（sign/verify 一律拒绝，不静默降级）。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from services.platform.security import (
    PLATFORM_SIG_PREFIX,
    PUBLISHER_SIG_PREFIX,
    PluginSigner,
    generate_signing_key,
    public_key_hex_of,
    sign_publisher_plugin,
    verify_platform_plugin,
    verify_publisher_plugin,
)

CHECKSUM = "c" * 64


def _server_json() -> dict[str, Any]:
    return {
        "name": "io.ontology-agent/weather",
        "display_name": "天气查询",
        "version": "1.0.0",
        "description": "按城市查天气",
        "transport": {"type": "streamable_http", "url": "https://weather.example.com/mcp"},
        "x-platform": {"schema_version": "1", "required_scopes": ["weather:read"]},
    }


def _signed_server_json(publisher_private_hex: str, checksum: str) -> dict[str, Any]:
    """模拟开发者上传件：manifest 内嵌开发者公钥与签名（平台签由平台在发布时回填）。"""
    server_json = _server_json()
    server_json["x-platform"]["publisher_public_key"] = public_key_hex_of(publisher_private_hex)
    server_json["x-platform"]["publisher_signature"] = sign_publisher_plugin(
        publisher_private_hex, server_json=server_json, checksum=checksum
    )
    return server_json


async def test_双签_真签全链_两级验签通过():
    # Arrange：开发者签（第一级）→ 平台签（第二级，对「发布者签名+清单」整体再签）
    publisher_private = generate_signing_key()
    server_json = _signed_server_json(publisher_private, CHECKSUM)
    publisher_signature = server_json["x-platform"]["publisher_signature"]
    platform_signer = PluginSigner(generate_signing_key())
    platform_signature = platform_signer.sign_platform(
        server_json=server_json, checksum=CHECKSUM, publisher_signature=publisher_signature
    )
    # Act / Assert：平台签格式 + 两级验签全过
    assert (
        platform_signature.startswith(PLATFORM_SIG_PREFIX) and len(platform_signature) == len(PLATFORM_SIG_PREFIX) + 128
    )
    assert platform_signer.verify_platform(
        server_json=server_json,
        checksum=CHECKSUM,
        publisher_signature=publisher_signature,
        platform_signature=platform_signature,
    )
    assert verify_publisher_plugin(
        server_json["x-platform"]["publisher_public_key"],
        server_json=server_json,
        checksum=CHECKSUM,
        publisher_signature=publisher_signature,
    )


async def test_平台签伪造_拒绝():
    publisher_private = generate_signing_key()
    server_json = _signed_server_json(publisher_private, CHECKSUM)
    platform_signer = PluginSigner(generate_signing_key())
    platform_signature = platform_signer.sign_platform(
        server_json=server_json, checksum=CHECKSUM, publisher_signature=server_json["x-platform"]["publisher_signature"]
    )
    forged = PLATFORM_SIG_PREFIX + "f" * 128  # 伪造签（非平台钥所签）
    assert not platform_signer.verify_platform(
        server_json=server_json,
        checksum=CHECKSUM,
        publisher_signature=server_json["x-platform"]["publisher_signature"],
        platform_signature=forged,
    )
    # 错钥验签同样拒绝（平台公钥必须与签发钥配对）
    assert not verify_platform_plugin(
        public_key_hex_of(generate_signing_key()),
        server_json=server_json,
        checksum=CHECKSUM,
        publisher_signature=server_json["x-platform"]["publisher_signature"],
        platform_signature=platform_signature,
    )


async def test_清单篡改_平台签与开发者签双双拒绝():
    publisher_private = generate_signing_key()
    server_json = _signed_server_json(publisher_private, CHECKSUM)
    platform_signer = PluginSigner(generate_signing_key())
    platform_signature = platform_signer.sign_platform(
        server_json=server_json, checksum=CHECKSUM, publisher_signature=server_json["x-platform"]["publisher_signature"]
    )
    tampered = copy.deepcopy(server_json)
    tampered["description"] = "描述被篡改（投递面替换）"
    assert not platform_signer.verify_platform(
        server_json=tampered,
        checksum=CHECKSUM,
        publisher_signature=server_json["x-platform"]["publisher_signature"],
        platform_signature=platform_signature,
    )
    assert not verify_publisher_plugin(
        server_json["x-platform"]["publisher_public_key"],
        server_json=tampered,
        checksum=CHECKSUM,
        publisher_signature=server_json["x-platform"]["publisher_signature"],
    )


async def test_开发者签缺失_拒绝():
    publisher_private = generate_signing_key()
    server_json = _signed_server_json(publisher_private, CHECKSUM)
    public_key = server_json["x-platform"]["publisher_public_key"]
    assert not verify_publisher_plugin(public_key, server_json=server_json, checksum=CHECKSUM, publisher_signature=None)
    assert not verify_publisher_plugin(public_key, server_json=server_json, checksum=CHECKSUM, publisher_signature="")
    # 换包摘要（checksum 不符）同样拒绝
    assert not verify_publisher_plugin(
        public_key,
        server_json=server_json,
        checksum="d" * 64,
        publisher_signature=server_json["x-platform"]["publisher_signature"],
    )


async def test_占位旧签与畸形签_拒绝():
    platform_signer = PluginSigner(generate_signing_key())
    server_json = _server_json()
    legacy_placeholder = f"{PLATFORM_SIG_PREFIX}{'b' * 64}"  # M5 首批占位签（64 位 hex，非真签）
    assert not platform_signer.verify_platform(
        server_json=server_json, checksum=CHECKSUM, publisher_signature=None, platform_signature=legacy_placeholder
    )
    assert not platform_signer.verify_platform(
        server_json=server_json, checksum=CHECKSUM, publisher_signature=None, platform_signature="not-a-signature"
    )


async def test_平台密钥缺失_fail_closed_签验一律拒绝():
    signer = PluginSigner(None)
    assert signer.ready is False
    server_json = _server_json()
    with pytest.raises(ValueError, match="fail-closed"):
        signer.sign_platform(server_json=server_json, checksum=CHECKSUM, publisher_signature=None)
    with pytest.raises(ValueError, match="fail-closed"):
        signer.verify_platform(
            server_json=server_json, checksum=CHECKSUM, publisher_signature=None, platform_signature="x"
        )
    with pytest.raises(ValueError, match="fail-closed"):
        _ = signer.public_key_hex


async def test_密钥格式非法_构造即拒绝():
    with pytest.raises(ValueError):  # 非 hex
        PluginSigner("zz" * 32)
    with pytest.raises(ValueError):  # 长度不足
        PluginSigner("ab" * 16)
    assert PluginSigner(generate_signing_key()).ready is True


async def test_签名字段本身不进签名域_回填后验签仍通过():
    # 发布联动把平台签回填进 x-platform.signature；签名域剔除签名字段本身，回填不改签名域
    publisher_private = generate_signing_key()
    server_json = _signed_server_json(publisher_private, CHECKSUM)
    signer = PluginSigner(generate_signing_key())
    signature = signer.sign_platform(
        server_json=server_json, checksum=CHECKSUM, publisher_signature=server_json["x-platform"]["publisher_signature"]
    )
    server_json["x-platform"]["signature"] = signature  # 上架态回填
    assert signer.verify_platform(
        server_json=server_json,
        checksum=CHECKSUM,
        publisher_signature=server_json["x-platform"]["publisher_signature"],
        platform_signature=server_json["x-platform"]["signature"],
    )
    assert PUBLISHER_SIG_PREFIX  # 前缀常量在位（登记册口径引用锚点）
