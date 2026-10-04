# AGENTS.md — ontology-agent 平台开发规范（AI 代理入口）

> 本文件是任何 AI 代理（Claude Code / pi / openclaw / 平台自托管 agent）在本仓库工作的上下文入口。保持精简：细节按下面的地图按需读取，不要整篇粘贴。
>
> ⚠ **2026-09-26 路线终裁（混合路线，用户拍板）**：**后端工程权威 = docs/architecture/**（七层：网关/业务/领域/语义/数据/基础 + 全生命周期文档体系）；**前端权威 = docs/架构设计/ 前端族**（03 设计系统/16 前端规格/22 工程规范/23 VibeCoding/24 组件库，React + Tailwind + shadcn/ui + Apple 液态玻璃）+ `frontend/src/design-system/` 样式库；**信息架构/页面清单/角色权限 = docs/frontend/02**（栈无关）。多会话并行纪律：动手前先读本文件；新文档编号 = 当期最大 +1；**禁止移动、归档、删除他人未裁决的产物**，路线冲突提交用户裁决，禁止代用户重启路线之争。

## 项目是什么

**ontology-agent**：以本体（Ontology）为语义基座的智能体平台。向上托管多种 agent 工具，中间以 graphrag-ontology 知识库 + 本体推理核心为语义中枢，向下以 MCP 为能力出口打通业务闭环（含业务回写 writeback 一等模块与 Agent 内核/能力层划分）。

**技术栈定稿**：后端 FastAPI 模块化单体**七层**（锚点：[docs/architecture/01](docs/architecture/01-总体架构与分层.md)），PG+Neo4j+Milvus+MinIO+Redis，部署 lite/full 双档；前端 **React + Tailwind + shadcn/ui**（Apple 液态玻璃，设计系统=[docs/架构设计/03](docs/架构设计/03-前端架构与UI设计规范.md)）。**目录轴=模块（services/<模块>/{api,domain,business,data}+全局 gateway/platform，2026-09-27 用户裁决，锚点 01 §4 已同步）**。**后端全部开发在 services/ 路径下（含平台 CLI=services/cli）——仓库根不设后端目录（2026-09-29 用户指令，锚点 01 §4 已同步）**。

## 设计宪法（全仓库强制，违反即返工）

1. OB2 建模法：对象—行为—规则，事件串联逆向闭环；
2. 推理分级：确定性高频逻辑走规则引擎/SHACL，LLM 只做低频语义判断且输出必须过校验；
3. 候选非成品：LLM 产物一律进审核队列，人工终审才生效（治理三档 solo/team/enterprise 调节强度，硬门禁任何档不可跳过）；
4. 最小够用：单体优先，wedge 竖线先行（电力停电分析场景），禁止过度设计；
5. 全程可追溯：知识带出处、动作带审计、变更带版本。

## 文档地图（按需读，不要全读；总入口 [docs/README](docs/README.md)）

| 要做什么 | 读什么 |
| ---- | ---- |
| 后端架构与分层（工程权威） | [docs/architecture/01 锚点](docs/architecture/01-总体架构与分层.md) → 02~08 各层与横切 + 两份评审记录 |
| 前端视觉/组件/工程 | [docs/架构设计/03](docs/架构设计/03-前端架构与UI设计规范.md)（设计系统）→ 16/22/23/24（规格/流程/组件库）+ frontend/src/design-system |
| 前端信息架构/页面/路由/角色 | [docs/frontend/02](docs/frontend/02-信息架构与页面设计.md)（栈无关权威） |
| 需求与产品 | [docs/product/](docs/product/README.md)（BRD/PRD/路线图） |
| 模块详设 | docs/Agent（含 02 内核/能力层划分权威）、ontology（含 02 重型本体优化）、OntRAG、memory、MCP（含业务回写）、Skills（含四通道能力层）、Sandbox（沙箱与执行环境，模块 13）；RSI/ORSI = [architecture/09](docs/architecture/09-平台自进化RSI设计.md)（模块 14，含 §12 二轮增补、§13 ORSI 总纲：本体驱动八大进化面+缺口轨+工具进化链路） |
| API 与协议（端点登记册=docs/api/01） | [docs/api/](docs/api/README.md)（REST/SSE/MCP/A2A） |
| 数据库 DDL / 编码规范 / 测试 / 运维 | docs/database、standards、testing、ops |
| 上游研究依据 | docs/研究整理/00~07（结论只引用不修改） |

## 技能（按任务加载，SKILL.md）

- 写前端代码前 → `services/skills/frontend-dev-standards/`
- 做界面/组件/样式 → `services/skills/frontend-ui-apple/`（与 React+Apple 设计系统同源，可直接用）
- 测试与视觉验收 → `services/skills/frontend-testing/`
- 提交/PR 前代码评审 → `services/skills/code-review-ocr/`（OpenCodeReview `ocr` CLI，AI 行级评审，LLM 已配 deepseek）

## 工程约定

- 分支与多 Agent 协作（2026-09-27 起，hooks 强制）：**主工作区=develop 集成区，禁止直接提交**（合并提交除外）；任何改动先 `sh services/tools/git/wt new <域>-<简述>` 开独立 worktree + feature 分支，完成后 `sh services/tools/git/wt finish [--push]` 串行合入；共享索引文件（各 README/AGENTS）**追加式编辑**；规范见 [docs/standards/02](docs/standards/02-git多Agent协作与Worktree规范.md)；commit 中文 conventional（`feat: xxx`，commit-msg hook 强制）；**提交纪律（2026-09-29 用户指令）：阶段性小步提交（一个逻辑单元一个 commit，勿积大包），信息必含五要素——做了什么/边界（范围与不做什么）/测试（门禁数值）/审核（ocr·人工·验收结论）/方案依据（设计文档章节）**；仓库根 `.gitmessage` 为模板（`git config commit.template .gitmessage` 已生效）。
- 前端铁律：令牌唯一事实源（frontend/src/design-system/tokens）、契约先行（端点登记册=docs/api/01）、组件清单优先、基元只读、小步可验证、截图验收闭环。
- 后端语言全栈 Python ≥3.11（ADR-1）；规则引擎 v1 = SPARQL CONSTRUCT + pySHACL + rdflib（owlrl）；TBox = MinIO 版本化 Turtle + rdflib；ABox = Neo4j（只读物化，非推理机）。
- 平台 LLM/工具调用一律带审计与 trace_id；MCP annotations 不作授权依据。
- 改表流程：06 篇契约 → database/01 DDL → Alembic 迁移，顺序不可反；种子数据唯一走 Alembic 数据迁移。

## 已知注意事项

- 首次代码提交前统一改名：`services/ → services/`、`docs/Agent/ → docs/Agent/`、`docs/memory/ → docs/memory/`、`docs/OntRAG/ → docs/OntRAG/`（零代码引用，零成本窗口）；
- 本机 → Gitee HTTPS 推 >1MB 单文件会被重置，大文件拆分提交；
- 本平台开发流程自身吃狗粮：`services/skills/` 即平台 Skills 服务的第一批资产，格式（agentskills.io）即为平台技能格式。
