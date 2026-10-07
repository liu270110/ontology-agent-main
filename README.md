# ontology-agent

以本体（Ontology）为语义基座的智能体平台（ontology-rsi-harness）：向上托管多种 agent 工具，向中以 graphrag-ontology 知识库 + 本体推理核心提供可信业务上下文与可校验逻辑约束，向下以 MCP 协议打通业务系统（含业务回写 writeback 一等模块），形成「知识感知 → 推理决策 → 执行反馈 → 知识更新」闭环。

**技术栈（2026-09-26 混合路线终裁）**：后端 FastAPI 模块化单体**七层**（工程权威=[docs/architecture/01](docs/architecture/01-总体架构与分层.md)），PG+Neo4j+Milvus+MinIO+Redis，部署 lite/full 双档；前端 **React + TypeScript + Tailwind + shadcn/ui**（Apple 液态玻璃，设计系统=[docs/架构设计/03](docs/架构设计/03-前端架构与UI设计规范.md) + frontend/src/design-system 样式库；信息架构沿用 [docs/frontend/02](docs/frontend/02-信息架构与页面设计.md)）。

## 文档导航（总入口 [docs/README](docs/README.md)）

| 要看什么 | 去哪里 |
| ---- | ---- |
| 系统架构（七层/技术选型/模块地图） | [docs/architecture/](docs/architecture/README.md) |
| 前端视觉与工程 | [docs/架构设计/](docs/架构设计/README.md) 03/16/22/23/24 篇 |
| 需求与产品（BRD/PRD/路线图） | [docs/product/](docs/product/README.md) |
| 各模块设计（Agent/本体/知识库/记忆/MCP/插件/CLI） | [docs/](docs/README.md) 总索引 |
| API 与协议（REST/SSE/MCP/A2A） | [docs/api/](docs/api/README.md) |
| 数据库 / 编码规范 / 测试 / 运维 / 用户手册 | docs/database、standards、testing、ops、user |
| 调研结论（上游依据） | [docs/研究整理/](docs/研究整理/README.md) |

## 工程约定

- **每个目录必须有 `README.md`**：文档目录 README = 索引；**代码目录 README = 模块 spec 六段式**（职责边界/目录结构/核心机制/接口契约/数据模型/开发指南与验收标准）。
- 架构变更先改 `docs/architecture/01` 锚点；契约变更先改对应权威篇（端点登记册=docs/api/01）。
- 分支（2026-09-27 起 worktree 工作流，hooks 强制）：主工作区=develop 集成区禁直提；`sh services/devtools/git/wt new <域>-<简述>` 开功能分支，`wt finish` 串行合入；详见 [docs/standards/02](docs/standards/02-git多Agent协作与Worktree规范.md)；commit 中文 conventional。

## 功能规范（Feature Spec · 四区）

> **上线纪律**：每次发版前必须更新本节与 [frontend/CHANGELOG.md](frontend/CHANGELOG.md)——两者不同步视为发版未完成。功能口径以本节为权威快照。

### 主页区 `/`（用户功能交互）
对话（SSE 流式+Markdown/代码块+工具卡+Artifact 卡+运行审批卡+停止生成+消息操作组+证据溯源+轨迹回放入口）｜群聊（多 Agent 编排·四路由模式·成员分组·高风险确认·拒绝/转人工）｜任务中心（七步流水线+日志+重试/取消+事件时间线）｜本体工作台（类树/画布/检查器/公理编辑/校验报告/版本评审 diff）｜知识库（八态管理+上传+回收站+库设置+抽取审核+检索 Playground+图谱浏览）｜记忆管理（L1-L4+升级终审+失效流+容量卡）｜新手引导卡（三步判定自动隐藏）

### 平台区 `/platform`（浏览·获取能力）
插件市场｜工具与技能｜MCP 接入｜Agent 目录｜能力总览卡

### 管理控制台 `/console`（治理）
审批中心（六类+批量+SLA 倒计时+多签进度+详情弹窗）｜系统管理（用户 CRUD/用户组/RBAC 矩阵/模型渠道/租户/审计日志/**系统日志四维检索+trace 瀑布**/回写台账人工处置/**成本看板**）｜邀请链接（局域网/公网口径）

### 用户设置 `/settings`（个性化）
资料（头像裁剪）/安全（2FA+TOTP 二维码）/API Keys/通知/设备/外观与语言/对话偏好/记忆偏好/数据导出/账号 Danger Zone/关于（版本·构建·双端）

### 桌面端（Electron）
oa:// 协议+多窗口（主页/管理控制台）+自动更新+托盘

### 上线检查清单（发版前逐项勾）
- [ ] 本节功能清单与实际一致（新增/变更/下线）
- [ ] frontend/CHANGELOG.md Unreleased → 版本号
- [ ] frontend/package.json 与 desktop/package.json 版本双端同步
- [ ] git tag v<版本> 打在 develop 合入点
- [ ] 三档部署其一完成冒烟（Docker/Linux/Windows，33 篇）

## 快速开始

```bash
pip install -r requirements.txt   # 当前仅测试依赖，后端完整依赖随 M1 补充
pytest -v
python -m services.main           # 统一入口；或 uvicorn services.gateway.app:create_app --factory
```

## CI/CD（Gitee Go）

流水线：`.workflow/develop-pipeline.yml`（push develop/master 触发；Python 3.9 + pytest——**待 M0 升级为 3.11+ 六阶段门禁**，见 docs/testing/01）。

## 项目结构

```
.
├── frontend/            # 前端（design-system 样式库；React 工程随 M5 搭建）
├── services/             # L2~L7 后端模块化单体（gateway/business/domain/semantic/data/infra；统一入口 services/main.py）
│   ├── cli/             # 平台 CLI（onto 命令；后端含 CLI 一律在 services/ 内）
│   ├── tools/           # 研究性工具（PoC 脚本/kb-eval/orsi/onto-train/openapi 快照/git 工作流）
│   └── skills/          # 前端开发技能（SKILL.md，与平台 Skills 服务同构）
├── deploy/              # docker-compose（lite/full 双档）与初始化脚本
├── docs/                # 全部文档（architecture=后端权威；架构设计=前端视觉权威；详见 docs/README）
├── resrch-pj/           # 参考项目区（只读研究）
└── tests/               # 跨层集成测试
```
