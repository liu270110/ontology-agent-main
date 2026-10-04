---
name: frontend-react-perf
description: React 性能与正确性规范（Vercel react-best-practices 70 规则的本仓适配子集）。触发：写/审组件、数据获取、渲染优化、包体问题。
metadata:
  source: 改写自 vercel-labs/agent-skills react-best-practices（MIT），SPA+react-query 栈适配
---

# React 性能规范（本仓适配）

> 栈事实：React 18 SPA（无 RSC/Next）+ @tanstack/react-query v5 + zustand + Vite。
> 原版 70 条中 RSC/Server Actions/after() 类规则标记【N/A】；与本仓「三层铁律」冲突时以铁律为准。

## 1. 数据获取（CRITICAL）

- [ ] **并行化**：互不依赖的请求 Promise.all / 多 useQuery 并行；禁止先 await A 再请求 B 的瀑布（react-query 多 query 天然并行，注意人为串联）
- [ ] **条件化启用**：分支里才需要的请求用 useQuery `enabled:` 延迟（如 Tab 未挂载不拉）
- [ ] **cheap 条件前置**：同步的便宜判断放在 await 之前
- [ ] 轮询用 refetchInterval 且页面不可见时暂停（react-query visibility 默认行为，勿覆盖）

## 2. 包体（CRITICAL）

- [ ] 重库懒加载：路由级 lazy（既有 S8 模式）+ manualChunks 分组（echarts/codemirror/xyflow/rjsf/markdown 已配，新重库跟进）
- [ ] 禁 barrel file 深潜：从具体文件 import，不从 index 桶文件拉整包
- [ ] 第三方脚本/大依赖延迟到交互后
- [ ] 动态 import 用于条件功能（如导出、裁剪器）

## 3. 重渲染（HIGH）

- [ ] 组件内不定义组件（no inline components）——提到模块级
- [ ] 向下传的非原始字面量 props 提升到模块级（空数组/默认对象）
- [ ] 派生状态在 render 里算（useMemo），不在 effect 里 setState 回写
- [ ] 回调用函数式 setState 保持引用稳定
- [ ] 高频输入：useDeferredValue 兜长渲染列表
- [ ] 只在回调里用的状态不要订阅（useRef 存瞬态）
- [ ] 独立依赖的 hook 拆开，避免大依赖数组互相触发
- [ ]昂贵列表行 React.memo + 稳定 props（本仓：ChatStream 长流、任务表）

## 4. 渲染正确性（HIGH）

- [ ] 条件渲染用三元，不用 `&&`（防 0/NaN 渲染）
- [ ] show/hide 高成本子树用条件卸载或 keepalive 策略明确说明
- [ ] 长列表 >50 行虚拟化（本仓暂无，新增长列表时评估 content-visibility）
- [ ] SVG 动画包在 div 上做；transform-origin 正确
- [ ] 动画只 transform/opacity；可中断；reduced-motion 降级

## 5. JS 微观（MEDIUM）

- [ ] 循环内：属性访问缓存、RegExp 提出、早退、length 前置
- [ ] 重复查找用 Map/Set（O(1)）
- [ ] filter+map 合并为一次遍历（大数据集）
- [ ] localStorage 读取缓存（每次读有序列化开销）

## 6. react-query 专项（本仓补充）

- [ ] queryKey 结构化且含全部参数（qk.* 工厂统一，禁止裸字符串拼参）
- [ ] 轮询：refetchInterval + 明确的业务上限；mutation 后 invalidate 精确 key
- [ ] 选择器：select 选项做子树订阅，避免整仓重渲
- [ ] 缓存共享：同数据多组件复用同 queryKey（onboarding 卡先例），不重复拉

## 7. 本仓既有约定（冲突时优先）

- 令牌唯一事实源（tokens.css）→ 性能改造不得引入硬编码样式绕过令牌
- 统一入口/统一配置层 → 禁直连 fetch 绕过 api client（401/信封/trace 逻辑会丢）
- 测试跟随全局 MSW setupFiles

## 审查模式

改完组件自查四问：瀑布在哪？谁会不必要重渲？包里多了什么？错误/空/加载三态齐吗？
