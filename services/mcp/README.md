# mcp（MCP 网关，模块 5）

M4 填充：FastMCP 出口 + 外部接入 + CapabilityProvider 注册（权威=docs/MCP）。

## M4.1 落地结构（2026-09-27，计划 4.1）

- `server.py`：FastMCP 出口工厂 `build_mcp_server(registry, *, audit_sink, granted_scopes)`——
  api/03 §3 权威七 tool + §3.8 memory.invalidate / §3.9 writeback.status；入参 Schema 由类型签名推导；
  统一调度 `_dispatch`（PDP scope 判定 → 注册表路由 → provider.invoke → 审计 → 错误映射）；
  外部 tool 动态挂载（远端 Schema 透传）。
- `registry.py`：CapabilityRegistry——平台 provider 注册（版本握手/命名空间去重）+
  `register_capability(name, handler, metadata)` 快捷面 + 外部 tool `{server}.{local}`
  强制前缀隔离（`RESERVED_NAMESPACES` 防冒充平台 tool）。
- `providers.py`：Ontology/Memory/Knowledge/Action/WritebackStatus 五 provider（07 篇 §1 依赖倒置
  L3/L5 侧适配）；制品装载器/检索协作对象/记忆仓储/回写执行面均以协议注入（mcp 禁入各模块 data/）。
- `bootstrap.py`：**能力装配共享工厂**（2026-09-27 M4 验收 P1-1 收口）——
  `build_capability_registry(session_factory, *, settings, artifact_loader=None)`：
  gateway lifespan 与独立进程**同一装配面**（knowledge/ontology/memory/action/writeback.status
  全量）；`PgInvocationAuditSink`（audit_logs PG 汇，NIL 租户/PG 不可用降级日志汇）随工厂承载；
  制品装载器为组合根注入参数（gateway 注入 PG 制品读面适配，独立进程未注入 → 5004 结构化降级）。
- `client/`：外部 MCP 接入——`targets.py`（配置文件注册表，`targets.example.json` 为样例）+
  `connectors.py`（Streamable HTTP/stdio 双连接器、TTL 发现缓存、超时硬兜底、熔断半开、出向审计）。
- `audit.py`：调用审计端口 + logging 缺省汇（PG 汇见 bootstrap）。
- `errors.py`：api/03 §8 错误映射（领域错误 → isError + 四字段错误体 JSON）。
- `__main__.py`：独立进程入口
  `python -m services.mcp --transport stdio|http [--targets ...] [--anonymous-scopes ...] [--tenant-id <uuid>]`；
  经共享工厂装配（与 gateway 同面；进程级 e2e=tests/mcp/test_standalone_e2e.py）。

依赖倒置协议=L7 `services/platform/ports/capability_provider.py`（CapabilityProvider/
CallContext/CapabilityDescriptor/CapabilityResult；版本握手 KERNEL_LOOP_VERSION）。
