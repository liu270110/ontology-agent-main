---
name: frontend-testing
description: 本仓库前端测试与视觉验收技能。完成 UI 改动后、修复样式问题、编写或更新测试时使用。规定截图验收闭环、视觉回归基线、Playwright 关键路径与测试分层约定。
---

# frontend-testing：前端测试与视觉验收

任何视觉改动的完成标准不是"代码写完"，而是**截图证明**。本技能定义验收闭环。

## 验收闭环（每次 UI 改动必走）

1. **启动**：后端未就绪时用 mock 模式（`pnpm dev:mock`，MSW）；需要全链路时用 `with_server.py` 同时拉起前后端。
2. **截图**：Playwright（chromium headless）对目标页面截四类图：
   - 主题 × 2：亮色、暗色（localStorage 切 `.dark` class）；
   - 状态 × 按适用：空态、加载态（路由级 Suspense/骨架）、错误态、关键交互后态。
3. **核对**：逐张对照 `docs/架构设计/03` 篇 §2 设计系统（层次/圆角/发丝线/字重/间距）；对照 23 篇 DoD"视觉"维度。
4. **入库**：截图存 `e2e/__screenshots__/{页面}/{状态}.png`，视觉基线如有更新须在 PR 说明原因。
5. **汇报**：截图路径 + 差异结论写进交付报告。

**修复循环上限**：截图不过 → 修 → 重截，最多 3 轮；3 轮不过立即停下向用户报告卡点（附前后对比图），禁止盲改。

## 测试分层（写测试前先定位在哪层）

| 层 | 工具 | 写什么 |
| ---- | ---- | ---- |
| 单元 | Vitest | `lib/` 纯函数、hooks（`renderHook`）、zod schema、状态机分支 |
| 组件 | Vitest + Testing Library | 用户行为（点击/输入/键盘），断言可见结果而非实现细节 |
| Storybook 交互测试 | story 测试 | 基元组件六态：默认/hover/禁用/加载/空/异常 |
| E2E | Playwright | 关键路径：登录→工作台→对话发送→本体建模保存→审核通过 |
| a11y | axe-core 挂 Playwright | 四个核心页面零 critical 违规 |

## 约定

- 网络模拟统一 MSW handlers（`src/mocks/`），测试内禁止自造 fetch mock；
- 不测样式细节（视觉回归管），测行为与状态；
- E2E 失败必须保留 trace 与截图；`--update-snapshots` 只允许在 PR 说明差异原因后使用；
- 本机无浏览器依赖时先 `pnpm exec playwright install chromium`。
