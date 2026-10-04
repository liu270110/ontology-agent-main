---
name: frontend-ui-audit
description: UI 代码审核清单（Vercel Web Interface Guidelines 本仓适配版，~90 条）。触发：视觉/编码审核关卡、提交前自查、"review my UI"。输出 file:line 格式。
metadata:
  source: 改写自 vercel-labs/web-interface-guidelines command.md，适配本仓（中文 UI/MSW/令牌）
---

# UI 审核清单（审核关卡用）

> 用法：对目标文件逐类过一遍，发现即记 `文件:行 类别:规则`。审完按「Anti-patterns」兜底扫一遍。本仓基线：中文 UI、液态玻璃令牌、react-query、全局 MSW。

## 可访问性

- 图标按钮必须有 aria-label；装饰图标 aria-hidden="true"
- 表单控件有 label（htmlFor 或包裹）或 aria-label；输入有 name+autocomplete+正确 type/inputmode
- 动作用 `<button>`、导航用 `<a>`/`<Link>`——永不 div onClick
- 异步反馈（toast/校验）aria-live="polite"；标题层级 h1→h6 不跳级
- 语义优先，ARIA 兜底；媒体有替代文本

## 焦点

- 一切可交互元素 focus-visible 可见（禁裸 outline-none 无替代）
- 复合控件用 :focus-within；浮层/吸顶不得遮挡焦点元素

## 表单

- 禁禁粘贴；label 可点；checkbox/radio 与 label 同命中区
- 提交钮保持可点直至请求发出，期间 spinner；错误行内展示+聚焦首个错误
- placeholder 以 … 结尾；未保存变更离开要拦截警告
- 本仓补充：危险操作走既有 Modal 确认模式；toast 反馈走 sonner

## 动效

- 尊重 prefers-reduced-motion；只动 transform/opacity；禁 transition:all（列出属性）
- 动画可中断；>5s 自动播放提供暂停；本仓动效走 motion.css 既有类与 --ease/--dur

## 排版（中文适配）

- 省略号用 …；数字列 tabular-nums；标题 text-wrap:balance
- 加载文案以…结尾；单位与数字间不换行
- 本仓：字号消费字阶六键，禁裸 text-[Npx]（残留白名单除外）

## 内容与容器

- 长内容：truncate/line-clamp/break-words；flex 子元素 min-w-0
- 空态优雅（.empty+动作）；考虑超短/超长输入
- 表格数字列对齐（mono+tabular）

## 图片与性能

- img 显式宽高防 CLS；折叠下方 loading=lazy
- 列表 >50 项虚拟化或 content-visibility；render 里禁 getBoundingClientRect
- 批量 DOM 读写不交叉；受控输入每次击键要便宜（大表单用非受控+提交收集）

## 导航与状态

- 筛选/Tab/分页/面板状态反映到 URL query（本仓先例：?tab=/?layer=/?job=）
- 深链全部有状态 UI；破坏性操作要确认或可撤销——永不立即
- 本仓：跨页跳转走 redirect 映射表（四区 IA），旧路径不许死链

## 触屏与浮层

- touch-action:manipulation；模态/抽屉 overscroll-behavior:contain
- 手势必须有点击+键盘替代；拖拽中禁文本选择
- 触点 ≥24px；autoFocus 克制（移动端禁）

## 暗色与主题

- html 有 color-scheme；theme-color meta 匹配底色；原生 select 显式底色字色
- 本仓：亮暗成对令牌；玻璃上文字有光晕承托；reduce-transparency 降级

## 水合与安全

- 受控输入有 onChange 或 defaultValue；日期渲染防水合错配
- 链接外跳 target=_blank + rel=noopener noreferrer nofollow
- dangerouslySetInnerHTML 只允许自家受信内容（注明出处注释）

## 文案

- 按钮文案具体（「保存 API Key」非「确定」）；错误信息含修复动作
- 中文 UI：主动语态、第二人称；计数用数字；空格节奏统一（中西文间留白感）

## Anti-patterns（出现即打回）

user-scalable=no / 禁粘贴 / transition:all / 裸 outline-none / div onClick 导航 /
div 当按钮 / 图片无尺寸 / 大 .map() 无虚拟化 / 输入无 label / 图标钮无 aria-label /
手写日期格式 / 无理由 autoFocus / GIF 当视频 / 纯手势无替代
