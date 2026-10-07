"""模块：MCP 网关（模块 5，权威=docs/MCP + docs/api/03-MCP工具契约.md；M4 计划 4.1 落地）。

出口（server.py）：FastMCP 七 tool + memory.invalidate 补充项，语义标注 _meta.x-ontology，
全 tool 审计 + PDP scope 判定（annotations 红线：仅 UI 提示不作授权）。
注册表（registry.py）：CapabilityRegistry——平台 provider 注册 + 外部 tool {server}.{local}
命名空间隔离（防冒充）；能力协议=L7 services/platform/ports/capability_provider.py。
外部接入（client/）：Streamable HTTP + stdio 双连接器、发现缓存、熔断半开（MCP 篇 §4）。
入口（__main__.py）：独立进程形态（python -m services.mcp），不改 gateway 组合根。
"""
