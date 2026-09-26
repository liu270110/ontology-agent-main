# OntRAG — 知识库 GraphRAG 设计文档

本目录对应平台**模块 #3：知识库 graphrag-ontology**（文档 → 实例抽取 → 检索，ABox 管理 + GraphRAG 检索），见架构锚点 §5 模块地图。

## 目录名与规范名对照

**目录名 `OntRAG` 是既有拼写误差，服务规范名为"知识库 graphrag-ontology"，目录规范名建议为 `OntRAG`**。架构锚点 §7 待办：首次代码提交前 `docs/OntRAG/ → docs/OntRAG/` 改名；改名后文档内相对链接需同步更新。

## 文档索引

| 文档 | 内容 |
| ---- | ---- |
| [知识库GraphRAG设计.md](./知识库GraphRAG设计.md) | ABox/TBox 边界、七步抽取流水线（含四大国标硬门禁）、GraphRAG 索引管线（Leiden 社区 + community report + 任务化增量）、local/global/drift 检索路由与 RRF 混合检索、knowledge.search 契约、四存储落库模型、人工终审对接、Playground 联调接口、验收标准 |
| [多源接入与连接器设计.md](./多源接入与连接器设计.md) | **姊妹篇：知识从哪来**——六类源全景（安全分区红线/最差可行路径）、连接器契约与统一信封、业务库零侵入三段式+分级 SLA（T0~T3）、跨系统语义消歧（source_system scope/谓词级 systemRelative/实体解析）、动态知识（活性环+knowledge.next_actions）、FDE 工具包与第一周清单、接入验收标准 |

## 上游依据

- [架构锚点：01-总体架构与分层](../architecture/01-总体架构与分层.md) —— 存储职责划分（PG/Neo4j/Milvus/MinIO/Redis）、L5 语义层定位；
- [研究整理/03-知识库与本体核心](../研究整理/03-知识库与本体核心.md) —— ABox/TBox 分工（§1）、七步流水线与四大国标底线（§3.1）、GraphRAG 机制与本体增强（§3.2）、存储选型（§3.3）。

## 关联目录

- [docs/ontology](../ontology/README.md) —— 本体核心（TBox/推理/SHACL/版本），本服务的校验与推理后端；
- [docs/Agent](../Agent/README.md) —— Agent 服务，knowledge.search 的消费方（经 MCP）。

## 术语速查

- **TBox / ABox**：术语层（类/属性/公理，本体核心管）/ 实例层（从文档抽取的实体关系，本目录管）；
- **local / global / drift**：GraphRAG 三种检索模式，路由规则见设计文档 §4.1；
- **四大国标底线**：候选非成品、术语唯一、规则人工把关、实例过 SHACL——流水线硬门禁（设计文档 §2）。

## 维护规则

- 设计文档与架构锚点冲突时，先改锚点再改本文；
- 七步流水线与四大国标底线为上游定稿结论，调整须先改研究整理 03；
- 每次实质修改递增状态行版本号并更新日期。
