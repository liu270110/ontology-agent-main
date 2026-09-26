# frontend/ — L1 前端工程（Web 控制台）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[架构锚点](../docs/architecture/01-总体架构与分层.md)、[前端设计文档](../docs/frontend/README.md)

## 1. 职责与边界

- 平台 Web 控制台（L1 前端层）：页面渲染、交互、状态管理、SSE 消费、图谱可视化（锚点 §2）。
- **只经网关 API 通信**（REST + SSE，`/api/v1`），禁止直连数据库、直连模型 API、绕过网关调内部服务。
- 不承载业务规则：校验结论（SHACL/推理/审核门禁）一律以后端返回为准，前端只做展示与输入格式校验。
- 本期「先设计后编码」：代码骨架按 docs/frontend 三篇文档搭建，M0 起步。

## 2. 技术栈

Vue ≥3.5 + TypeScript ≥5.6 + Vite ≥6；Pinia（状态）+ Vue Router 4（路由/守卫）；Element Plus 2.9+（主题定制）；AntV G6 5（图谱浏览）/ X6 2（建模画布）；ECharts 5（成本看板）；axios 1.7。选型理由见锚点 §3.1。

## 3. 目录结构规划

```text
frontend/
├── index.html  vite.config.ts  .env.*          # 工程配置与代理
└── src/
    ├── main.ts  App.vue                        # 入口
    ├── api/            # 接口层：axios 封装 + 按网关模块分文件（唯一发请求处）
    ├── stores/         # Pinia：auth/ui/session/ontology/kb/memory/plugin
    ├── router/         # 路由表（对齐 docs/frontend/02 §2）+ 权限守卫
    ├── views/          # 页面：chat/tasks/kb/ontology/memory/agents/extensions/system
    ├── components/     # 全局复用组件：layout/graph/chat/review/common 五组
    ├── composables/    # useEventStream（SSE）/ useTheme / usePermission
    ├── styles/         # tokens.scss（--onto-* 唯一来源）+ element-overrides.scss
    ├── utils/  assets/ # 纯函数与静态资源（图谱八类型图标）
```

每层职责细节见 [docs/frontend/03-前端工程架构](../docs/frontend/03-前端工程架构.md) §2（本 README 不重复展开）。

## 4. 核心机制要点

- **主题 token 机制**：`styles/tokens.scss` 定义全部 `--onto-*`（亮 `:root` / 暗 `html.theme-dark`），`element-overrides.scss` 把 Element Plus 的 `--el-*` 映射到 `--onto-*`；组件禁写裸色值，亮/暗切换 = html class 切换（docs/frontend/01 §3）。
- **SSE 消费**：`useEventStream` composable 统一消费对话/任务事件流（AG-UI 事件语义蓝本，见 docs/研究整理/02），自动重连带 `Last-Event-ID`、心跳超时看门狗、事件分发进 Pinia store（docs/frontend/03 §5）。
- **权限路由**：路由表带 `meta.roles`，全局守卫校验 + 动态菜单过滤 + `v-permission` 按钮级控制，角色集合 `super_admin/admin/ontologist/curator/member`（docs/frontend/02 §1.1）。
- **图谱可视化**：浏览用 G6、建模编辑用 X6，共用八色板适配层 `graphPalette.ts`；图数据由网关返回统一 `{nodes,edges}` 结构（docs/frontend/03 §8）。
- **可观测**：所有请求透传 `trace_id`，错误 Toast 与审计页可展示全链路 ID（锚点 §6.7）。

## 5. 关联文档

| 文档 | 内容 |
| ---- | ---- |
| [docs/frontend/01-主题与设计系统](../docs/frontend/01-主题与设计系统.md) | Onto Blue 主题、Design Token、Element Plus 覆盖、图谱配色 |
| [docs/frontend/02-信息架构与页面设计](../docs/frontend/02-信息架构与页面设计.md) | 站点地图、路由表、P1~P12 逐页线框/组件/跳转流 |
| [docs/frontend/03-前端工程架构](../docs/frontend/03-前端工程架构.md) | 工程目录、API/SSE 封装、状态管理、构建联调、代码规范 |
| [架构锚点](../docs/architecture/01-总体架构与分层.md) | 分层、技术选型、协议栈（REST + SSE）、里程碑 |

## 6. 开发里程碑与验收标准（对齐锚点 §7）

| 里程碑 | 内容 | 验收 |
| ---- | ---- | ---- |
| M0 骨架 | Vite 工程 + tokens.scss + Element Plus 覆盖 + AppLayout 壳 + 登录页 | 亮/暗切换全站生效，lint/build 绿 |
| M1 布局与导航 | 路由/守卫/动态菜单 + 403/404 全局错误页 | 能登录、菜单按角色过滤 |
| M2 知识库与本体（锚点 M2 语义层 wedge） | P2 任务列表 + P3 文档管理 + P4 抽取审核 + P5 检索 Playground + P6 建模工作台（X6）+ P7 版本评审 | 建模→抽取→审核→检索全流程可走（对齐锚点 M2 出口「建模→抽取→检索 demo 跑通」） |
| M3 对话与记忆（锚点 M3 Agent+记忆） | P1 对话（SSE 流式）+ P2 任务事件时间线 + P9 记忆 + P10 Agent 管理 | 能对话、有记忆、有引用（对齐锚点 M3 出口） |
| M4 扩展中心 | P11 扩展中心（工具 / 插件 / 外部 MCP） | 外部 MCP 接入与工具纳管可用 |
| M5 前端全量 | P8 图谱浏览器（G6）+ P12 系统管理 + 全部页面按设计稿验收 + 性能与暗色回归 | 对齐锚点 M5「按设计稿验收」；六大导航组全部可用 |

> 2026-09-26 评审修复F：里程碑表按锚点 §7 重排——M2=知识库与本体（语义层 wedge 先行）、M3=对话（Agent+记忆）；原表「M1 对话最小版 / M3 本体与图谱」与锚点里程碑次序冲突（对话编排属 M3、本体建模属 M2），已纠正。P8 按 FR-ONTO-08（M5）留在 M5。

验收通用标准：docs/frontend/01 §7（主题验收清单）+ docs/frontend/03 §12（工程验收清单）逐条通过。

## 7. 待办与开放问题

- [ ] M0 前先落 `pnpm create vite` 骨架与 CI 前端构建阶段（锚点待办）。
- [ ] SSE 鉴权传递（stream ticket）与网关对齐（docs/frontend/03 §13）。
