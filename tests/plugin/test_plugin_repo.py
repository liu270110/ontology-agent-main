"""plugin PG 仓储与全链集成测试（本地 PG；不可达/未迁移自动跳过）。

覆盖：六态→三值投影 roundtrip、in_review 由 open 工单重建、发布联动、
install 绑定默认停用与启停持久化、租户隔离（tools 唯一索引含 tenant_id）、slug 唯一 4507。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from tests.plugin.helpers import publisher_signed_fields
from sqlalchemy.ext.asyncio import AsyncSession

from services.platform.security import PLATFORM_SIG_PREFIX, PluginSigner
from services.plugin.business.lifecycle import PluginMarketService
from services.plugin.data.repo_impl.plugin_repo import PgPluginRepository, PgToolBindingRepository
from services.plugin.domain.model.plugin import Plugin, PluginKind, PluginStatus, PluginVersion
from services.review.business.candidates import ReviewApprovalService, ReviewTicketService
from services.review.data.governance import PgGovernanceTierReader

if TYPE_CHECKING:
    from tests.plugin.conftest import PluginSeed

pytestmark = pytest.mark.integration

CHECKSUM = "d" * 64
PLATFORM_SIGNER = PluginSigner("ab" * 32)  # 进程内 dev key（发布真签联动）


def _server_json() -> dict:
    return {
        "name": "io.ontology-agent/it-weather",
        "display_name": "IT 天气",
        "version": "1.0.0",
        "description": "集成测试插件",
        "transport": {"type": "streamable_http", "url": "https://weather.example.com/mcp"},
        "x-platform": {
            "schema_version": "1",
            "category": "data-tools",
            "required_scopes": ["weather:read"],
            "compatible_protocol_versions": ["2025-06-18"],
            "tools": [
                {
                    "name": f"it.weather.{uuid.uuid4().hex[:8]}",
                    "description": "查询",
                    "input_schema": {"type": "object"},
                    "required_scopes": ["weather:read"],
                }
            ],
        },
    }


def _signed_server_json() -> dict:
    """带开发者签名的清单（发布联动前置——平台签对象=发布者签名+清单，Skills §5.1）。"""
    server_json = _server_json()
    server_json["x-platform"].update(publisher_signed_fields(server_json, CHECKSUM))
    return server_json


def _market(seed: PluginSeed, db: AsyncSession) -> PluginMarketService:
    """每会话装配（仓储构造期绑定会话与租户；审批面走 PG 工单服务）。"""
    tickets = ReviewTicketService(seed.factory)
    return PluginMarketService(
        repo=PgPluginRepository(db, seed.tenant_id),
        bindings=PgToolBindingRepository(db, seed.tenant_id),
        review=tickets,
        approvals=ReviewApprovalService(tickets, PgGovernanceTierReader(seed.factory)),
        runtime=seed.runtime,
        signer=PLATFORM_SIGNER,
    )


async def _create_and_publish(
    seed: PluginSeed, db: AsyncSession, slug: str, server_json: dict
) -> tuple[Plugin, PluginVersion, PluginMarketService]:
    service = _market(seed, db)
    plugin, ver = await service.create_listing(
        tenant_id=seed.tenant_id,
        publisher_id=seed.publisher_id,
        slug=slug,
        name="IT 插件",
        kind=PluginKind.MCP_SERVER,
        version="1.0.0",
        server_json=server_json,
        artifact_key=f"plugin-packages/{slug}/1.0.0/package.zip",
        checksum=CHECKSUM,
    )
    ticket_id = await service.submit_for_review(
        tenant_id=seed.tenant_id, plugin_id=plugin.id, version_id=ver.id, submitter_id=seed.publisher_id
    )
    outcome = await service.review_decision(
        tenant_id=seed.tenant_id, ticket_id=ticket_id, action="approve", approver_id=seed.publisher_id
    )
    assert outcome.published is True
    return plugin, ver, service


async def test_插件仓储_roundtrip_draft与版本_发布后投影published(plugin_seed):
    # Arrange
    seed = plugin_seed
    async with seed.factory() as db:
        repo = PgPluginRepository(db, seed.tenant_id)
        plugin = Plugin(
            slug=f"it-{uuid.uuid4().hex[:10]}",
            name="IT 插件",
            kind=PluginKind.MCP_SERVER,
            publisher_id=seed.publisher_id,
        )
        ver = PluginVersion(
            plugin_id=plugin.id,
            version="1.0.0",
            server_json=_server_json(),
            artifact_key=f"plugin-packages/{plugin.id}/1.0.0/package.zip",
            checksum=CHECKSUM,
        )
        # Act
        await repo.add(plugin)
        await repo.add_version(ver)
        await db.commit()
        loaded = await repo.get(plugin.id)
        versions = await repo.list_versions(plugin.id)
        # Assert：draft 投影 + 版本树
        assert loaded is not None and loaded.status is PluginStatus.DRAFT
        assert loaded.slug == plugin.slug and len(versions) == 1
        # Act：发布链 → 存储投影 published（suspended/deprecated 同投影见模型测试）
        ver.record_scan({"passed": True})
        ver.publish()
        plugin.submit()
        plugin.pass_auto_gates()
        plugin.publish()
        await repo.save_version(ver)
        await repo.save(plugin)
        await db.commit()
        reloaded = await repo.get(plugin.id)
        # Assert
        assert reloaded is not None and reloaded.status is PluginStatus.PUBLISHED


async def test_上架全链_PG_in_review由open工单重建_发布联动runtime(plugin_seed):
    # Arrange / Act
    seed = plugin_seed
    slug = f"it-{uuid.uuid4().hex[:10]}"
    async with seed.factory() as db:
        service = _market(seed, db)
        plugin, ver = await service.create_listing(
            tenant_id=seed.tenant_id,
            publisher_id=seed.publisher_id,
            slug=slug,
            name="IT 插件",
            kind=PluginKind.MCP_SERVER,
            version="1.0.0",
            server_json=_signed_server_json(),
            artifact_key=f"plugin-packages/{slug}/1.0.0/package.zip",
            checksum=CHECKSUM,
        )
        ticket_id = await service.submit_for_review(
            tenant_id=seed.tenant_id, plugin_id=plugin.id, version_id=ver.id, submitter_id=seed.publisher_id
        )
        await db.commit()
        # Assert：solo 档 open 工单在审 → 读侧重建 in_review（08 §4 工单承载流程态）
        detail = await service.get_detail(plugin.id)
        assert detail[0].status is PluginStatus.IN_REVIEW
        # Act：solo 自批（提交人即审批人）→ 发布联动
        outcome = await service.review_decision(
            tenant_id=seed.tenant_id, ticket_id=ticket_id, action="approve", approver_id=seed.publisher_id
        )
        published = await service.get_detail(plugin.id)  # 仓储回读（发布联动改写的是重建实例）
        published_signature = published[0].signature
        await db.commit()
    # Assert：工单 published、插件 published、runtime 已注册（候选非成品出口门禁）
    assert outcome.published is True
    assert seed.runtime.get(plugin.id) is not None
    # Assert：PG 链发布即平台真签（占位签退役；PG 回填 server_json 持久化经 save_version）
    assert published_signature is not None and published_signature.startswith(PLATFORM_SIG_PREFIX)


async def test_安装绑定_默认停用_enable持久化_租户隔离(plugin_seed):
    # Arrange：走完发布链
    seed = plugin_seed
    slug = f"it-{uuid.uuid4().hex[:10]}"
    async with seed.factory() as db:
        plugin, _ver, service = await _create_and_publish(seed, db, slug, _signed_server_json())
        # Act：安装（tools.enabled=false 默认——外部默认不可信）
        bindings = await service.install(tenant_id=seed.tenant_id, plugin_id=plugin.id)
        await db.commit()
        assert len(bindings) == 1 and bindings[0].enabled is False
        # Act：enable → 持久化
        enabled = await service.enable(tenant_id=seed.tenant_id, plugin_id=plugin.id)
        await db.commit()
        assert all(b.enabled for b in enabled)
        # Assert：他租户仓储不可见（租户隔离；tools 唯一索引含 tenant_id）
        foreign = PgToolBindingRepository(db, uuid.uuid4())
        assert await foreign.list_by_plugin(plugin.id) == []
        assert await foreign.get_by_name(bindings[0].name) is None


async def test_slug_全平台唯一_4507(plugin_seed):
    # Arrange
    seed = plugin_seed
    slug = f"it-{uuid.uuid4().hex[:10]}"
    async with seed.factory() as db:
        service = _market(seed, db)
        await service.create_listing(
            tenant_id=seed.tenant_id,
            publisher_id=seed.publisher_id,
            slug=slug,
            name="首个",
            kind=PluginKind.MCP_SERVER,
            version="1.0.0",
            server_json=_server_json(),
            artifact_key=f"plugin-packages/{slug}/1.0.0/package.zip",
            checksum=CHECKSUM,
        )
        # Act / Assert：同 slug 再登记 → 4507（uk_plugins_slug 前置检查）
        with pytest.raises(Exception) as exc_info:
            await service.create_listing(
                tenant_id=seed.tenant_id,
                publisher_id=seed.publisher_id,
                slug=slug,
                name="重名",
                kind=PluginKind.MCP_SERVER,
                version="1.0.0",
                server_json=_server_json(),
                artifact_key="plugin-packages/x/1.0.0/package.zip",
                checksum=CHECKSUM,
            )
        assert "4507" in str(exc_info.value)
