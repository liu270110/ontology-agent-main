"""插件域模型测试：上架状态机（六态负向）、版本不变式、存储投影、门禁 1 schema 校验。"""

from __future__ import annotations

import uuid

import pytest

from services.platform.kernel import DomainError
from services.plugin.domain.model.manifest import manifest_tools, validate_manifest
from services.plugin.domain.model.plugin import (
    Plugin,
    PluginKind,
    PluginStatus,
    PluginVersion,
    PluginVersionStatus,
    from_storage_status,
    to_storage_status,
)

CHECKSUM = "a" * 64


def _plugin(**kw) -> Plugin:
    base: dict = {"slug": "weather", "name": "天气插件", "kind": PluginKind.MCP_SERVER}
    base.update(kw)
    return Plugin(**base)


def _version(plugin: Plugin, **kw) -> PluginVersion:
    base: dict = {
        "plugin_id": plugin.id,
        "version": "1.0.0",
        "artifact_key": f"plugin-packages/{plugin.id}/1.0.0/package.zip",
        "checksum": CHECKSUM,
    }
    base.update(kw)
    return PluginVersion(**base)


def test_上架状态机_合法全链_发布后停用恢复再弃用():
    # Arrange
    p = _plugin()
    # Act / Assert：draft → submitted → in_review → published
    assert p.status is PluginStatus.DRAFT
    p.submit()
    assert p.status is PluginStatus.SUBMITTED
    p.pass_auto_gates()
    assert p.status is PluginStatus.IN_REVIEW
    p.publish()
    assert p.status is PluginStatus.PUBLISHED
    # published ↔ suspended（启停/运行期异常）
    p.suspend()
    assert p.status is PluginStatus.SUSPENDED
    p.resume()
    assert p.status is PluginStatus.PUBLISHED
    # → deprecated 终态
    p.deprecate()
    assert p.status is PluginStatus.DEPRECATED


def test_上架状态机_负向_draft直接发布拒绝():
    # Arrange
    p = _plugin()
    # Act / Assert
    with pytest.raises(DomainError) as exc:
        p.publish()
    assert int(str(exc.value)[:4]) == 4501


def test_上架状态机_负向_deprecated为终态_不可复活():
    # Arrange
    p = _plugin()
    p.submit()
    p.pass_auto_gates()
    p.publish()
    p.deprecate()
    # Act / Assert：终态任何迁移拒绝
    for op in (p.submit, p.publish, p.suspend, p.resume, p.deprecate):
        with pytest.raises(DomainError):
            op()


def test_上架状态机_负向_draft不可停用_review中不可发布():
    # Assert：suspended 仅自 published
    p = _plugin()
    with pytest.raises(DomainError):
        p.suspend()
    # Assert：in_review 可发布但 draft 不可 suspended/deprecated
    p2 = _plugin()
    p2.submit()
    p2.pass_auto_gates()
    p2.reject_back_to_draft()
    assert p2.status is PluginStatus.DRAFT


def test_版本不变式_负向_发布后不可回扫描_delisted终态():
    # Arrange
    v = _version(_plugin())
    v.record_scan({"gate": "schema_v1"})
    v.publish()
    assert v.status is PluginVersionStatus.PUBLISHED
    # Act / Assert：published → submitted 非法（发布后不可覆盖，04 篇 plugin 不变式）
    with pytest.raises(DomainError) as exc:
        v.record_scan({})
    assert int(str(exc.value)[:4]) == 4501
    # delisted 终态
    v.delist()
    with pytest.raises(DomainError):
        v.publish()


def test_存储投影_六态到三值_roundtrip_in_review由open工单重建():
    # Assert：六态 → 三值单一收敛点
    assert to_storage_status(PluginStatus.DRAFT) == "draft"
    assert to_storage_status(PluginStatus.SUBMITTED) == "draft"
    assert to_storage_status(PluginStatus.IN_REVIEW) == "draft"
    assert to_storage_status(PluginStatus.PUBLISHED) == "published"
    assert to_storage_status(PluginStatus.SUSPENDED) == "published"
    assert to_storage_status(PluginStatus.DEPRECATED) == "delisted"
    # 读侧重建：draft + open 工单 → in_review（08 §4 工单承载流程态）
    assert from_storage_status("draft", has_open_review=False) is PluginStatus.DRAFT
    assert from_storage_status("draft", has_open_review=True) is PluginStatus.IN_REVIEW
    assert from_storage_status("published", has_open_review=False) is PluginStatus.PUBLISHED
    assert from_storage_status("delisted", has_open_review=False) is PluginStatus.DEPRECATED
    with pytest.raises(DomainError):
        from_storage_status("unknown", has_open_review=False)


def test_门禁1_schema校验_缺必填4503_合法清单通过并标注未建关卡():
    # Assert：缺 transport/required_scopes → 4503（服务端实跑，任何档不可跳过）
    with pytest.raises(DomainError) as exc:
        validate_manifest({"name": "io.ontology-agent/weather", "display_name": "天气"})
    assert int(str(exc.value)[:4]) == 4503
    # Act：合法清单（§3.1 主轨最小形状）
    report = validate_manifest(
        {
            "name": "io.ontology-agent/weather",
            "display_name": "天气查询",
            "version": "1.2.0",
            "description": "按城市查天气",
            "transport": {"type": "streamable_http", "url": "https://weather.example.com/mcp"},
            "x-platform": {
                "schema_version": "1",
                "category": "data-tools",
                "required_scopes": ["weather:read"],
                "tools": [
                    {
                        "name": "weather.query",
                        "description": "查询天气",
                        "input_schema": {"type": "object"},
                        "required_scopes": ["weather:read"],
                    }
                ],
            },
        }
    )
    # Assert：通过 + 2~6 关如实标注未建（不伪称全绿）
    assert report["passed"] is True
    assert len(report["gates_pending"]) == 5


def test_manifest_tools_提取_声明顺序保持():
    # Arrange
    server_json = {
        "x-platform": {
            "tools": [
                {"name": "weather.query", "description": "查询", "required_scopes": ["weather:read"]},
                {"name": "weather.alert", "semantic_annotation": {"action_iri": "http://x#Alert"}},
            ]
        }
    }
    # Act
    tools = manifest_tools(server_json)
    # Assert
    assert [t.name for t in tools] == ["weather.query", "weather.alert"]
    assert tools[1].ontology_action_iri == "http://x#Alert"


def test_插件id判等_聚合实体纪律():
    # Arrange
    p = _plugin()
    other = _plugin()
    other.id = p.id
    # Assert：按 id 判等（04 篇 §1 实体纪律）
    assert p == other and hash(p) == hash(other)
    assert p != _plugin()
    assert uuid.UUID(str(p.id)) == p.id
