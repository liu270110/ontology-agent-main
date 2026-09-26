# 研究整理 - 调研结论层（上游依据）

> 定位：平台所有设计文档的**上游依据**。各篇结论一旦被设计文档引用，即成为设计的事实基线；修订需先改这里、再传导下游（见 docs/README §4）。

## 文档索引

| 篇 | 主题 | 主要被引用于 |
| ---- | ---- | ---- |
| [00-平台总体架构](./00-平台总体架构.md) | 平台定位、核心服务清单、核心闭环、设计原则（OB2/推理分级等五条宪法） | architecture/01 锚点及全部模块设计 |
| [01-Agent工具调研](./01-Agent工具调研.md) | nanobot / openclaw / hermes agent / claude / pi 五工具横向对比与托管建议 | docs/Agent/Agent服务设计 |
| [02-Agent服务与协议](./02-Agent服务与协议.md) | 模型协议（OpenAI/Anthropic）、Agent 协议（MCP/A2A/AG-UI）、网关选型 | architecture/02、07；docs/Agent、MCP |
| [03-知识库与本体核心](./03-知识库与本体核心.md) | TBox/ABox 分界、OB2+国标建模、推理分级、七步抽取流水线、GraphRAG、存储选型 | architecture/05、06；docs/ontology、OntRAG |
| [04-多层记忆](./04-多层记忆.md) | CoALA 四分类、四层记忆设计、沉淀/检索/遗忘管线 | architecture/06；docs/memory |
| [05-MCP平台能力](./05-MCP平台能力.md) | MCP 出口/接入/业务集成三形态、安全红线（annotations 不可信） | architecture/07、08；docs/MCP |
| [06-插件与工具集](./06-插件与工具集.md) | 插件市场/工具注册/server.json 主轨/沙箱隔离/审核上架 | architecture/07、08；docs/Skills |
| [07-Harness-Agent解剖与本体骨架](./07-Harness-Agent解剖与本体骨架.md) | harness 核心模块/机制/痛点；本体三面骨架；本体原生 agent loop 运行时；能力层可插拔（四通道+八扩展点，DSec 参照） | 待设计文档引用 |

## 维护规则

- 新调研新增编号文件（`08-xxx.md` 起），并在上表登记。
- 本层结论与下游设计冲突时：**以设计为准要先改这里**——流程是先更新本层结论（注明修订原因），再改下游文档。
