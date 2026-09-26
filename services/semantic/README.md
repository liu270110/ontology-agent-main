# semantic/ — L5 语义层（平台特色能力层）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[架构锚点](../../docs/architecture/01-总体架构与分层.md)
> 细节权威：docs/architecture/05-语义层设计.md；领域文档：docs/ontology/、docs/OntRAG/（已定稿）；上游研究：docs/研究整理/03

## 1. 职责与边界

- 平台语义基座，**被 L3 调用的能力层**：本体核心 `ontology_core`（TBox、推理、SHACL、版本治理、术语对齐）+ 知识库 `knowledge`（抽取、GraphRAG 检索）。
- 禁止：处理会话/权限等非语义事务（那是 L2/L3 的事）。

## 2. 目录结构规划

```text
semantic/
├── ontology_core/
│   ├── tbox/         # 类/属性/公理/规则的内存模型与读写（权威：rdflib 制品）
│   ├── reasoning/    # 推理分级路由（规则/SHACL → LLM 低频复杂语义）
│   ├── shacl/        # pySHACL 校验封装与违例报告
│   ├── versioning/   # 变更单 diff、发布、回滚（TBox 制品版本）
│   └── align/        # 术语对齐（跨本体/同义词）
└── knowledge/
    ├── extract/      # 文档→候选三元组/实例（七步流水线的语义步）
    ├── graphrag/     # 社区检测/摘要、local/global/drift 检索策略
    └── retrieve/     # Neo4j 图检索 + Milvus 向量混合召回
```

## 3. 核心机制要点

- **对外能力接口**：`ontology.validate / ontology.reason / ontology.diff / ontology.publish`、`knowledge.extract / knowledge.search`，L3 与 MCP 能力注册只认这些接口。
- **推理分级路由**（平台宪法，锚点 §6.4）：确定性强的高频逻辑走规则引擎/SHACL（毫秒级确定性）；低频复杂语义才走 LLM；**LLM 输出必须过规则校验**后作为候选产物。
- **权威与物化分离**：权威 TBox 是 rdflib/文件制品（MinIO）；Neo4j 是属性图物化视图（经 n10s），Neo4j 不是推理机，一致性校验必须走本层推理引擎（锚点 §3.2）。
- **候选非成品**：抽取产物、LLM 对齐建议一律输出为候选，交 L3 review_workflow 终审。

## 4. 接口契约

- **依赖**：L6（Neo4j/Milvus/MinIO 客户端）、L7（llm 能力）、L4 领域服务接口（门禁实现）；经 CapabilityProvider 把 `ontology.*`/`knowledge.*` 注册进 L7 MCP 网关（倒置点之二）。
- **被谁调用**：L3 的 chat_orchestrator（检索/校验）、kb_pipeline（抽取）、review_workflow（校验门禁）。

## 5. 数据模型要点

- OWL/Turtle 本体制品（MinIO，版本化）；Neo4j：ABox 实体/关系/社区 + TBox 属性图物化；Milvus：切片向量、社区摘要向量；候选产物带 `confidence` 与来源指针（文档/分片/原文偏移）。

## 6. 开发指南与验收标准

- 能力接口入出参一律 dataclass/pydantic，不暴露 rdflib Graph 对象跨界；SHACL 违例报告结构化（节点/路径/约束/严重级）供前端定位。
- 验收（对齐锚点 M2）：本体 CRUD + SHACL 校验 + rdflib 推理可用；抽取流水线最小版产出候选；Neo4j/Milvus 入库与混合检索 demo（建模→抽取→检索）。

## 7. 待办与开放问题

- [ ] OWL 推理引擎 PoC：rdflib+pySHACL vs 外挂 Jena Fuseki（锚点待办、研究整理 03 遗留）。
- [ ] GraphRAG local/global/drift 三模式的参数与成本基线测定。
- [ ] 与 docs/ontology、docs/OntRAG 两篇领域文档的编写与对齐（本文不重复其细节）。
