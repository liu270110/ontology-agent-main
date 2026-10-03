---
name: frontend-deslop
description: 去除前端 UI 的「AI 生成感」（AI slop）——在 ontology-agent 的既定品牌（iOS26 液态玻璃）内做出有态度的设计决策。触发：新页面/组件视觉设计、风格审查、"看起来像 AI 做的"。
metadata:
  source: 改写自 samber/cc-skills/frontend-design-deslop v1.2.2（MIT），适配本仓液态玻璃品牌裁决
---

# 去除 AI 生成感（ontology-agent 适配版）

AI UI 显得平庸的根因：无约束时模型采样「近期网页代码的统计中位数」（蓝紫渐变、Inter 字体、均质卡片）。本仓已有**确定的品牌系统**（iOS26 液态玻璃三档 + 令牌体系），因此去 AI 味的重点不是换风格，而是**在既定系统内做出明确决策、消灭无意识的默认值**。

## 与本仓品牌的冲突裁定

- 原版禁令「禁 glassmorphism」**不适用**：液态玻璃是用户裁决的品牌语言（三档纪律见 03 篇 §3.0）。玻璃用得对=品牌；用得错=无层次滥用。判据：每处玻璃是否声明了档位（Regular 承载文字/Clear 悬浮控件/Tinted 强调）且文字有光晕承托。
- 原版禁令「禁渐变文字/紫渐变」**仍适用**：品牌强调色是 --accent（iOS 蓝），不是紫渐变。
- 其余禁令（bounce 缓动、动画布局属性、纯色表意、<24px 触点）全部适用。

## 检查清单（设计/审查时逐条过）

### 排版
- [ ] 主字体是品牌字体栈（-apple-system/PingFang SC），未退化到 Inter/Roboto/system-ui 泛用栈
- [ ] 字号只消费字阶六键（text-2xs/xs/13px/15px/base/lg），无裸 text-[Npx]
- [ ] 数字列用 tabular-nums；标题用 text-wrap:balance
- [ ] 中文排版：行高 ≥1.6、中西文间有空隙感、标点不悬挂在行首

### 色彩
- [ ] 60-30-10：中性面 60% / 品牌面 30% / 强调 10%——强调色不是到处撒
- [ ] 语义色只用令牌（--green/--orange/--red/--teal 及 soft 变体），无裸 hex
- [ ] 亮暗成对：每个新色都在 tokens.css 有 .dark 对应值
- [ ] 状态不用纯色表意（色弱可用）：徽标必须带文字

### 组件
- [ ] 每个交互组件有完整状态矩阵：hover/active/focus-visible/disabled/loading/empty
- [ ] 焦点可见：focus-visible 有 ring，永不裸 outline-none
- [ ] 触点 ≥24px；图标按钮有 aria-label
- [ ] 空态有设计（.empty 模式+下一步动作），不是一行灰字

### 动效
- [ ] 只动 transform/opacity；永不 transition:all
- [ ] 时长 ≤300ms、ease-out 族（--ease）；prefers-reduced-motion 降级
- [ ] 动效传达信息（进场层级/状态变化），不是纯装饰循环

### 内容
- [ ] 长文本容器有 truncate/line-clamp/break-words + min-w-0
- [ ] 空值有占位语义（—），不是空白
- [ ] 按钮文案具体（「保存 API Key」而非「确定」）；错误信息带下一步
- [ ] 数字/日期走 Intl 格式化，无手写格式串

### AI 味黑名单（出现即打回）
蓝紫渐变、渐变文字、均质三栏卡片阵、inter/roboto、emoji 当图标、bounce 缓动、
transition:all、outline-none 无替代、div onClick 当按钮、无维度装饰性渐变背景、
glassmorphism 无档位纪律（本项目特化条款）。

## 产出要求

- 设计决策记录进 DESIGN.md（本仓对应 docs/架构设计/03 篇 + design-system 令牌注释）
- 自审：对照本清单逐条过，结果附在提交说明（ocr review 会复核）

## 审查要点（review 模式）

对每个新页面/组件问四个问题：
1. 「如果遮住 logo，这是本产品还是任意 AI 生成页？」——差异点在哪
2. 「每个视觉决策能追溯到令牌或 03 篇条款吗？」——无出处即默认值
3. 「砍掉这个玻璃/徽标/渐变，信息是否仍完整？」——是则砍（装饰冗余）
4. 「状态矩阵缺哪个角？」——补齐再交
