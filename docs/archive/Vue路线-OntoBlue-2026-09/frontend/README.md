# 前端设计文档（docs/frontend）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[架构锚点](../architecture/01-总体架构与分层.md)

## 1. 目录定位

本目录是 **L1 前端层的设计文档**：把前端「先定主题、UI 设计先行」的三件事落成文——主题设计系统、页面与跳转设计、工程架构。前端只经网关 API（REST + SSE）与后端通信，技术选型（Vue 3 + TS + Vite + Pinia + Element Plus + AntV G6/X6）以架构锚点为准。

## 2. 阅读顺序（主题 → 页面 → 工程）

| 顺序 | 文档 | 回答的问题 |
| ---- | ---- | ---- |
| ① | [01-主题与设计系统](./01-主题与设计系统.md) | 长什么样？Onto Blue 主题、`--onto-*` token、亮暗机制、图谱八色板 |
| ② | [02-信息架构与页面设计](./02-信息架构与页面设计.md) | 有哪些页面？路由表、逐页线框/组件/跳转流、全局组件关联 |
| ③ | [03-前端工程架构](./03-前端工程架构.md) | 怎么实现？目录规划、API/SSE 封装、状态与权限、构建联调 |

主题是组件的输入，页面设计是工程实现的输入：**改主题先改 01，改页面先改 02，再同步代码**。

## 3. 与 frontend/ 代码目录的关系

- `frontend/` 是 L1 前端工程（本仓库根目录），其 `README.md` 是代码侧模块 spec（目录结构、核心机制、里程碑）。
- 对应关系（设计章节 → 代码落点）：

| 设计文档章节 | 代码落点 |
| ---- | ---- |
| 01 篇 §2 Design Token 表 | `frontend/src/styles/tokens.scss` |
| 01 篇 §3 Element Plus 覆盖 | `frontend/src/styles/element-overrides.scss` |
| 02 篇 §2 路由表 | `frontend/src/router/routes.ts` |
| 02 篇 §4 全局组件清单 | `frontend/src/components/` |
| 03 篇 §2 工程目录规划 | `frontend/src/` 整体骨架 |
| 03 篇 §5 SSE 封装 | `frontend/src/composables/useEventStream.ts` |

- 冲突裁决顺序：架构锚点 > 本目录三篇 > `frontend/README.md` > 代码注释。

## 4. 设计变更流程

1. 提 issue / 在 [代办任务](../代办任务/) 登记，说明改动影响面（token 改动必须列全亮/暗两态新值与对比度验证）。
2. 修改对应设计文档并在文首状态行升版本（v0.x → v0.x+1）。
3. 代码 PR 必须引用文档变更链接；纯代码样式调整不允许偏离 token（见 01 篇 §7 验收清单）。
4. 里程碑验收（见 [frontend/README](../../frontend/README.md) §6）以「按设计稿验收」为准，出人处以设计文档截图对比说明。

## 5. 待办与开放问题

- [ ] docs/architecture/ 前端相关结论（如 SSE 网关约定）落地后回填本目录引用。
- [x] ~~与旧稿的合并裁决~~（2026-09-26：React/Apple 旧稿已归档 docs/archive/，本目录即唯一前端设计权威）。
