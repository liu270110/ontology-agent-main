# MCP —— MCP 网关设计文档

> 本目录对应平台核心服务：**MCP 网关**——平台能力出口（MCP Server）、外部能力接入（MCP Client/Host）、业务系统集成三形态。
> 代码落点：`services/infra/mcp_gateway/`（server / client / adapter / registry / a2a）（架构锚点 §4/§5 模块 5）。

## 文档索引

| 文档 | 内容 |
| ---- | ---- |
| [MCP网关设计.md](./MCP网关设计.md) | 完整设计：双向定位、平台 MCP Server 工具清单、CapabilityProvider 注册机制、外部 Server 管理、业务系统集成闭环、安全治理、协议实现（Streamable HTTP / stdio / 版本协商）、A2A Agent Card、数据模型、验收标准 |

## 上游依据

- [架构锚点：01-总体架构与分层](../architecture/01-总体架构与分层.md)（分层、FastMCP 选型、依赖倒置例外 §3.4）
- [研究整理/02-Agent服务与协议](../研究整理/02-Agent服务与协议.md)（MCP 规范演进、A2A v1.0、协议互补关系）
- [研究整理/05-MCP平台能力](../研究整理/05-MCP平台能力.md)（双向定位、能力清单、annotations 安全红线）

## 关联文档

- [docs/ontology](../ontology/README.md)——`ontology.*` 工具的上游能力（TBox/推理）
- [docs/Skills](../Skills/README.md)——外部 MCP Server 封装上架（05→06 衔接）、统一工具注册中心
- [docs/Agent](../Agent/README.md)——平台内 agent 工具经本网关获取平台能力

## 阅读顺序

1. 先读架构锚点 §3.3（协议栈）与 §3.4（依赖倒置例外：CapabilityProvider）；
2. 再读本目录《MCP网关设计.md》§1~§3（三形态、工具清单、注册机制）；
3. 开发前重点核对 §6 安全治理（annotations 红线）、§7 协议实现（版本协商）、§9 数据模型。

## 维护规则

- 本文与架构锚点冲突时，以架构锚点为准；
- MCP 协议版本演进（2025-06-18 / 2025-11-25 / 2026-07-28）变动时，先更新研究整理 02/05，再同步本文 §7；
- 修改任何文档须同步更新其状态行；待办完成后打勾并注明结论。
