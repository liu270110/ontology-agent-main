# writeback（业务回写 action_dispatcher，模块 12）

M4 填充：行动实例化/幂等/对账/补偿（权威=docs/MCP/业务回写设计）。

## 计划 4.2 交付（2026-09-27）

- `domain/model.py`：WritebackLedger 聚合（状态机 pending→accepted→succeeded/failed/compensated
  + unknown 分支，只前进不回退）+ WritebackAction 行动实例 + OutboxEvent 信封；
- `business/action_dispatcher.py`：幂等键 `{tenant}:{action_instance}`（台账先行 + UK 硬兜底）、
  重试/超时 unknown/query_status 核实、补偿（compensate）、对账查询面（reconcile 报告）、
  writeback.status 查询面（`status`：三键任一定位 + tenant 过滤 + 未找到 404 语义，api/03 §3.9；
  `refresh_status` 为其 ledger_id 单键形态）；
- `business/relay.py`：Outbox relay（at-least-once，发布/标记非原子由消费端按 event_id 去重）；
- `business/policy.py`：WritebackPolicy（重试/退避/超时/对账时限/relay 参数，策略可配注入）；
- `adapters/base.py`：BizSystemAdapter 协议 + ConnectorRegistry（§5.2 元数据四项声明）；
- `adapters/mock_power_ticket.py`：电力工单 Mock（幂等重放/超时/限流/脏数据/状态不一致注入）；
- `data/orm.py` + `data/repo_impl/writeback_repo.py`：writeback_ledger / outbox_events 两表
  （迁移 b8d4e6f8a0c2，DDL 权威=database/01 §3.8）与 PG 仓储；
- 对外出口：action.invoke 与 writeback.status 经 `services/mcp/providers.py`
  （ActionCapabilityProvider / WritebackStatusCapabilityProvider）注册进 MCP registry
  （协议注入，组合根=共享工厂 services/mcp/bootstrap.py，gateway 与独立进程共用）。
