# docs - 平台文档总索引（全生命周期）

> 本目录是 ontology-agent 平台**从需求到运维全生命周期文档**的唯一入口。文档分三层：原始资料 → 调研结论 → 工程与产品文档，依赖方向单向（下层不改上层）。

## 1. 软件生命周期 × 文档地图

| 生命周期阶段 | 文档 | 目录 | 状态 |
| ---- | ---- | ---- | ---- |
| **需求** | BRD 商业需求 / PRD 产品需求 / 路线图 | [product/](./product/README.md) | v0.1 |
| **设计·UI（视觉权威）** | React+Apple 设计系统/组件库/样式库（架构设计/03、16、22、23、24 + frontend/src/design-system） | [架构设计/](./架构设计/README.md) | v1.3 |
| **设计·UI（信息架构权威）** | 页面清单/路由/角色/组件关联（栈无关） | [frontend/02](./frontend/02-信息架构与页面设计.md) | v0.1 |
| **设计·系统架构** | 总体架构锚点 + 七层设计 + 横切规范 + 评审记录 | [architecture/](./architecture/README.md) | v1.0 + 评审修订 |
| **设计·模块详设** | Agent / 本体 / 知识库 / 记忆 / MCP（含回写）/ 技能插件 / 沙箱执行环境 | [Agent/](./Agent/README.md) [ontology/](./ontology/README.md) [OntRAG/](./OntRAG/README.md) [memory/](./memory/README.md) [MCP/](./MCP/README.md) [Skills/](./Skills/README.md) [Sandbox/](./Sandbox/README.md) | v0.1~v0.2 |
| **设计·接口与协议** | REST 契约 / SSE 事件流 / MCP 工具契约 / A2A | [api/](./api/README.md) | v0.1 |
| **设计·数据库** | 全表 DDL / ER 图 / 分期建表 | [database/](./database/README.md) | v0.1 |
| **开发规范** | 编码规范（后端/前端/SQL/Git/评审清单） | [standards/](./standards/README.md) | v0.1 |
| **测试** | 测试策略 / 测试用例集（144 条，分域） | [testing/](./testing/README.md) | v0.1 |
| **部署运维** | 部署手册 / 运维手册（监控/备份/8 例 Runbook） | [ops/](./ops/README.md) | v0.1（目标态，M0 回填） |
| **用户使用** | 用户操作手册（12 页面全覆盖） | [user/](./user/README.md) | v0.1 |
| **工具** | CLI 设计 | [cli/](./cli/README.md) | v0.1 |
| **研究依据** | 调研结论（上游） | [研究整理/](./研究整理/README.md) | 持续收录 |
| **研究依据** | 原始资料归档 | [研究资料/](./研究资料/README.md) [Dsec/](./Dsec/README.md) | 归档 |
| **项目管理** | 任务清单与里程碑跟踪 | [代办任务/](./代办任务/README.md) | 使用中 |
| 🗄 归档 | 历史路线裁决产物（详见 archive/README） | [archive/](./archive/README.md) | 勿作开发依据 |

## 2. 权威与引用顺位

1. **同一事实只有一个权威**：分层与选型=architecture/01 锚点；**REST 端点登记册=docs/api/01**（协议机制/错误码/SSE=architecture/02 篇）；MCP=MCP 篇；数据契约=architecture/06 篇（docs/database 为 DDL 展开）；治理档位=08 篇 §2.4；领域模型与状态机=04 篇；需求事实=product/02 PRD。
2. 需求变更先改 PRD 再传导设计；架构变更先改 01 锚点（流程见 architecture/README §3）；契约变更先改对应权威篇再同步 docs/api。
3. 研究整理/研究资料为上游只读层：修订设计只改下游，除非结论本身错了（先改上游再传导）。

## 3. 阅读路径

- **新人/开发 agent**：`architecture/01`（锚点）→ 所做模块的设计文档 → `api/` 或 `database/`（契约）→ 对应代码目录 README（模块 spec）。
- **产品视角**：`product/01 BRD` → `product/02 PRD` → `frontend/02 页面设计`。
- **测试**：`testing/01 策略` → `testing/02 用例集`（用例是开发验收合同）。
- **部署/运维**：`ops/01 部署` → `ops/02 运维`（目标态标注 M0 回填）。
- **只想要全局图**：`architecture/01` 一篇 + 本索引。

## 4. 文档规范（铁律）

1. **每个目录必须有 `README.md`**；文档目录 README = 索引；**代码目录 README = 模块 spec 六段式**（职责与边界/目录结构/核心机制/接口契约/数据模型/开发指南与验收标准），随开发随更。
2. 统一格式：头部引用块（状态 | 日期 | 上游依据链接）、`## N.` 章节、结论标注出处、结尾待办 checkbox。
3. 发现文档间冲突：**不改汇编层、回改权威层**；无法当场裁决的登记进 [代办任务/](./代办任务/README.md)。
