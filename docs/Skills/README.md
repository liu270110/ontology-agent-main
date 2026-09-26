# Skills —— 技能与插件设计文档

> 本目录对应平台核心服务：**插件市场 + 工具集 + 技能**——插件管"商品"（打包/上架/分发/生命周期），工具注册中心管"单件"（工具元数据/权限/调用统计）。
> 代码落点：`services/business/plugin_lifecycle/`、`services/domain/model/{plugin,tool}`、`services/infra/plugin_runtime/`（架构锚点 §4/§5 模块 6）。

## 文档索引

| 文档 | 内容 |
| ---- | ---- |
| [技能与插件设计.md](./技能与插件设计.md) | 完整设计：tool / skill / plugin 三资产模型、工具注册中心与 Tool Search、插件格式（server.json 主轨 + OpenAPI 辅轨 + SKILL.md 分区）、审核流水线与生命周期、运行时隔离、权限模型、数据模型、验收标准 |

## 上游依据

- [架构锚点：01-总体架构与分层](../architecture/01-总体架构与分层.md)（插件运行时沙箱归属 L7、模块地图）
- [研究整理/01-Agent工具调研](../研究整理/01-Agent工具调研.md)（各 agent 工具技能体系兼容性）
- [研究整理/05-MCP平台能力](../研究整理/05-MCP平台能力.md)（外部 MCP 封装上架衔接、annotations 安全红线）
- [研究整理/06-插件与工具集](../研究整理/06-插件与工具集.md)（三资产分工、server.json 主轨、Dify plugin-daemon 隔离、审核五关门禁）

## 关联文档

- [docs/MCP](../MCP/README.md)——插件主轨即 MCP Server；外部 MCP Server 封装上架的衔接点
- [docs/ontology](../ontology/README.md)——工具语义标注关联本体行动类

## 阅读顺序

1. 先读架构锚点 §5 模块 6 与 §6 横切约束（候选非成品、annotations 红线）；
2. 再读本目录《技能与插件设计.md》§1~§2（三资产模型、工具注册中心）；
3. 开发前重点核对 §4 审核流水线（五关门禁）、§5 运行时隔离、§7 数据模型。

## 维护规则

- 本文与架构锚点冲突时，以架构锚点为准；
- 插件清单格式变更须先过"待办：x-platform 扩展字段定稿评审"，再同步本文 §3；
- 修改任何文档须同步更新其状态行；待办完成后打勾并注明结论。
