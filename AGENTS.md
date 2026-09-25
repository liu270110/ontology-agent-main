# AGENTS.md — ontology-agent 平台开发规范（AI 代理入口）

> 本文件是任何 AI 代理（Claude Code / pi / openclaw / 平台自托管 agent）在本仓库工作的上下文入口。保持精简：细节按下面的地图按需读取，不要整篇粘贴。

## 项目是什么

**ontology-agent**：以本体（Ontology）为语义基座的智能体平台。向上托管多种 agent 工具，中间以 graphrag-ontology 知识库 + 本体推理核心为语义中枢，向下以 MCP 为能力出口打通业务闭环。架构定稿见 [docs/架构设计/](docs/架构设计/README.md)（后端 FastAPI 模块化单体六层；前端 React + Tailwind + shadcn/ui，**Apple 风格**）。

## 设计宪法（全仓库强制，违反即返工）

1. OB2 建模法：对象—行为—规则，事件串联逆向闭环；
2. 推理分级：确定性高频逻辑走规则引擎/SHACL，LLM 只做低频语义判断且输出必须过校验；
3. 候选非成品：LLM 产物一律进审核队列，人工终审才生效；
4. 最小够用：单体优先，禁止过度设计；
5. 全程可追溯：知识带出处、动作带审计、变更带版本。

## 文档地图（按需读，不要全读）

| 要做什么 | 读什么 |
| ---- | ---- |
| 后端任何模块开发 | docs/架构设计/00（总览）、01（模块设计）、02（存储） |
| 前端 UI / 组件 / 页面 | docs/架构设计/03（设计系统全量定义） |
| 前端工程流程 / 测试 / CI | docs/架构设计/04 |
| 前端任务的标准作业流 | docs/架构设计/05（VibeCoding：SOP + DoD + 反模式） |
| 上游研究依据 | docs/研究整理/00~06 |
| 平台数据模型 | docs/架构设计/02 |

## 前端技能（按任务加载，SKILL.md）

- 写前端代码前 → `skills/frontend-dev-standards/`
- 做界面/组件/样式 → `skills/frontend-ui-apple/`
- 测试与视觉验收 → `skills/frontend-testing/`

## 工程约定

- 分支：日常开发在 `develop`，功能分支 `feature/{域}-{简述}`；commit 中文 conventional（`feat: xxx`）。
- 前端铁律：令牌唯一事实源（`theme/tokens.css`）、组件清单优先、基元只读、契约先行（OpenAPI 生成类型）、小步可验证、截图验收闭环（详见 23 篇六条铁律）。
- 后端语言全栈 Python（ADR-1）；规则引擎 v1 = SPARQL CONSTRUCT + pySHACL + owlready2；TBox = MinIO 版本化 Turtle + rdflib；ABox = Neo4j。
- 平台 LLM/工具调用一律带审计与 trace_id；MCP annotations 不作授权依据。

## 已知注意事项

- 仓库历史目录有拼写（`sevices/`、`docs/Agenrt/`、`docs/memorry/`、`docs/Otghrag/`），首次代码提交前统一改名；
- 本机 → Gitee HTTPS 推 >1MB 单文件会被重置，大文件拆分提交；
- 本平台开发流程自身吃狗粮：`skills/` 即平台 Skills 服务的第一批资产，格式（agentskills.io）即为平台技能格式。
