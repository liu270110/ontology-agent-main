---
name: frontend-dev-standards
description: 本仓库前端开发规范技能。写任何前端代码（页面/组件/hooks/测试/配置）之前使用。规定技术栈约定、目录与命名、Git 流程、性能预算与 DoD 自检。
---

# frontend-dev-standards：前端开发规范

写本仓库任何前端代码前先过一遍本技能。全量规范见 `docs/架构设计/22-前端工程规范与研发流程.md`；本文是可执行摘要。

## 技术栈（不得自行替换）

React 18 + TypeScript(strict) + Vite + Tailwind CSS 4 + shadcn/ui（源码在仓库内）+ TanStack Query（服务端状态）+ Zustand（客户端状态）+ React Router 7 + framer-motion + react-hook-form/zod。API 类型只允许 `openapi-typescript` 生成物，禁止手写接口类型。

## 目录与命名

```
frontend/src/
├── app/          # 路由表、AppShell、providers
├── theme/        # 设计令牌（唯一颜色事实源）
├── components/   # ui/（shadcn 定制基元，只读）+ business/（跨域业务组件）
├── features/     # 按域组织：chat/ontology/kb/memory/... 每域 pages/components/hooks/stores
├── api/          # 生成 client + query hooks（queryKey 三段式集中管理）
├── stores/       # 全局 Zustand
└── lib/          # 工具、错误码映射
```

- 组件目录 PascalCase + `index.tsx`；hooks `useXxx.ts`；路由 path 小写中划线。
- 跨 feature import 禁止相对路径——复用组件必须提升到 `components/business/`。
- 中文文案集中 `locales/zh-CN.ts`；query key 一律登记在 `api/queries.ts`。

## 代码规则

- `strict: true`；禁 `any`（用 `unknown` + 收窄）、禁非空断言 `!`。
- 数据获取必须处理四态：loading（Skeleton）/ empty（给动作入口）/ error（原因 + 建议）/ success。
- mutation 必须有 pending 反馈；副作用清理（取消竞态用 AbortController/Query 自带取消）。
- 依赖纪律：新增 dependency 要说明体积与替代方案；禁 moment、lodash 全量引入（用 `es-toolkit` 按需或原生）。

## Git 与交付

- 分支：从 `develop` 拉 `feature/frontend-{域}-{简述}`；commit 中文 conventional（`feat: 对话页消息流虚拟滚动`）。
- 单次交付 ≤ 一个页面或 ≤ 3 个组件；PR 附四张自检清单（视觉/逻辑/性能/a11y）。
- 性能预算：首包 JS ≤ 200KB gzip；React Flow/ECharts 路由级懒加载；列表 > 200 行虚拟化。

## 完成前必须做

1. `pnpm typecheck && pnpm lint && pnpm test` 全绿；
2. 过 DoD 六维清单（`docs/架构设计/05` §5：功能/视觉/类型/测试/性能/a11y）；
3. 加载 `frontend-testing` 技能做截图验收（亮/暗 × 空态/加载态/错误态）；
4. 按 23 篇 §4.1 模板出交付报告。
