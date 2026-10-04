---
name: frontend-ui-apple
description: 本仓库前端 UI 实现技能（Apple 风格）。做界面、组件、页面、样式调整时使用。规定如何消费设计令牌、复用组件清单、按 Apple 设计语言组装界面并避免 AI 模板审美。
---

# frontend-ui-apple：Apple 风格 UI 实现

本仓库前端是 React 18 + TypeScript + Tailwind 4 + shadcn/ui，视觉目标是"像 macOS/iOS 原生应用"。本技能告诉你怎么把界面做对、做得像。

## 硬约束（先读）

1. **令牌唯一事实源**：颜色/字号/间距/圆角/阴影/动效只允许引用 `frontend/src/theme/tokens.css` 的 CSS 变量（Tailwind 类名形式，如 `bg-surface text-label rounded-card`）。禁止硬编码 `#hex`、任意值 `bg-[#xxx]`。新增令牌先改 tokens.css。
2. **组件清单优先**：动手前列出需要的组件，逐一对照 `components/ui/`（shadcn 定制基元）与 `components/business/`（业务组件）。复用优先；新增组件必须在交付报告中说明"清单里为什么不够"。基元组件文件（`components/ui/`）禁止顺手修改。
3. **反 AI 模板审美，但方向是 Apple**：禁止紫色渐变/白底紫按钮/居中堆砌/emoji 图标/自创花哨字体。**豁免说明**：系统字体栈（SF Pro/苹方/雅黑）是本项目的品牌决策，必须使用，不得替换为 Inter 等字体。

## Apple 视觉速查（细节以 docs/架构设计/03 篇 §2 为准）

- **底色层次**：亮色 `--bg #f5f5f7` 上浮白色 `--surface` 卡片；暗色 `#000` 底 + `#1c1c1e` 卡片。侧边栏/顶栏用毛玻璃：`backdrop-blur-xl` + 半透明 surface + `saturate(180%)`。
- **文字**：层级靠字重字号（large-title 30/700 收紧字距 → headline 17/600 → body 15/400），次级文字用 `--label-2 #6e6e73`，不靠加灰色边框区分。
- **形状**：卡片 18px 圆角、控件 10px、CTA 胶囊形；分隔线用 0.5px 发丝线（`--separator`），能用发丝线就不加阴影边框双保险。
- **阴影**：极柔双层（`shadow-card`），hover 轻微抬升；禁止生硬大投影。
- **颜色**：全站单强调色 `--accent`（蓝），语义色只表状态；图标线条风，线宽 1.5px。
- **动效**：进出场 200–400ms `ease-apple` 曲线 + 位移/透明度；弹窗弹簧缩放（framer-motion stiffness 300 / damping 30）；列表 stagger；一律响应 `prefers-reduced-motion`。
- **布局**：内容区 `max-w-[1440px]`，左右 32px；空态必须给下一步动作；文案动词短语（"新建本体"）。

## 流程（每次 UI 任务）

1. 找到该页面/组件在 `docs/架构设计/03-前端架构与UI设计规范.md` 的定义（页面结构、组件关联、跳转）；没有定义 → 先补 spec 请用户确认，不要直接编码。
2. 组件盘点（硬约束 2），产出复用/新增清单。
3. 实现：基元 + 业务组件组装，数据走 TanStack Query，本地态 Zustand，路由按 03 篇 §4 路由表。
4. 自检 + 截图：加载 `frontend-testing` 技能执行验收闭环（亮/暗 × 关键状态）。
5. 按 23 篇 §4.1 交付报告模板汇报。

## 图谱与图表

- 图谱画布用 React Flow：节点分类色从 `theme.ts` 的分类色板取（purple/teal/indigo 等），证据路径高亮用 `--accent`；小地图默认收起。
- ECharts 必须挂载 `theme.ts` 导出的 Apple 主题对象，禁止图表默认配色直接上屏。
