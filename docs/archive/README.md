# docs/archive - 历史方案归档

> 本目录存放**已裁决废弃的并行方案**，仅作历史追溯，**禁止作为开发依据**。现行唯一权威：[docs/architecture/01-总体架构与分层](../architecture/01-总体架构与分层.md)。

## 归档清单

| 目录 | 历史定位 | 归档裁决 | 增量去向 |
| ---- | ---- | ---- | ---- |
| [架构设计-React路线-2026-09/](./架构设计-React路线-2026-09/README.md) | 并行产出的另一套架构定稿（README+00~23 共 25 篇，React + Tailwind + Apple 风格，六层），commit 265ff2a / 565daf7 | 2026-09-26 全量文档验收评审裁决：留 Vue + Onto Blue 路线（docs/architecture/），本套整体归档 | B 类增量（知识治理四型分诊/双队列记忆调度/IAM 审批中心/vLLM 推理服务/RSI 自进化/沙箱五场景/提示词治理等）的合入清单登记在 [../代办任务/](../代办任务/README.md)，未合入前不得引用本套实现细节 |
| [设计稿-Apple风格-2026-09/](./设计稿-Apple风格-2026-09/README.md) | Apple 风格 UI 设计看板（ui-design-board.html） | 同上（与 Onto Blue 主题冲突） | 线框交互思路可参考，视觉规范勿用 |

> 架构设计-React路线-2026-09/ 内含 `frontend-design-system-src/`（原 frontend/src/design-system 的 Apple 令牌 CSS 资产），一并归档；Vue 路线前端工程从零搭（[frontend 工程架构](../frontend/03-前端工程架构.md)）。

## 为什么归档（裁决记录）

2026-09-26 五专家全量文档验收（[评审记录](../architecture/评审-2026-09-26-全量文档验收与闭环确认.md)）确认：两套架构并存且 AI 入口（AGENTS.md）指向旧套，会导致所有 AI 代理按废弃路线工作（P0）。用户裁决：保留已过评审修订、配套全生命周期文档的 Vue + Onto Blue 路线，旧套归档。
