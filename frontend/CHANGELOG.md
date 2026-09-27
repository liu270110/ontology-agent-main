# 更新日志（CHANGELOG）

> 版本纪律：**版本单源** = `frontend/package.json` 的 `version` 字段；`desktop/package.json` 同步维护（发布冒烟需 grep 双端一致）。
> 构建期经 vite `define` 注入 `__APP_VERSION__` / `__BUILD_TIME__`，展示于 **设置 → 资料 → 关于** 卡（桌面端额外显示 Electron 壳版本与平台，经 preload `appInfo` 桥）。
> 版本规则：semver——`0.x` 阶段每功能批次 minor +1，缺陷修复 patch +1；每版本对应一个 git tag `v<version>`（打在 develop 合入点）。

所有显著变更记录于此。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### 计划中

- Tooltip 基元在业务页铺开使用点（基元已入库）
- 群聊/工作流域接入 ErrorState/Skeleton（组件已就绪，纯接线）
- 资源行级下载/@引用等 M4 端点对接（download-url / presigned）

## [0.2.0] - 2026-09-28

### 新增

- **iOS26 液态玻璃主题落地**：全局极光双光斑底（app-stage）、侧边栏玻璃配方（glass-side：tint 渐变 + blur24 saturate200）、顶栏材质条（material-bar：blur20 saturate180）、主按钮 Tinted 玻璃化（.btn-p 全站传播）、toast/batchbar/⌘K/Sheet 浮层 Clear·Regular 分档、文字光晕承托（--glass-halo）与 backdrop-filter/减透明度/暗色三重降级
- **Dify 式链接邀请**：邀请弹窗双模式（邮箱保留/链接新增：角色 + 有效期 24h·7d·30d + 生成 + 复制 + 已生成列表 + 撤销）；成员表「链接邀请」来源徽标；登录页 `?join=<token>` 受邀提示条（有效绿条/失效红条）与注册后自动加入；MSW 五端点（POST/GET/DELETE/preview/join，410 失效语义；DELETE 回 200 信封体——client 不解析空体）
- **Agent 工作区面板**：对话页右栏「上下文/工作区」双页签——文件树（teal 目录/dirty 橙点/预览抽屉/休眠回收倒计时）、受限终端（白名单 ls/pwd/cat/head/tail）、资源四分组（--src-* 着色）；MSW 四端点
- **工作区 SSE 实时联动**：workspace.file.created/modified/deleted、terminal.output 四事件贯通（消息流系统行/文件树防抖刷新/终端游标追加）；资源行 @引用插入消息草稿
- **全站状态组件**：ErrorState（错误态+重试）/ SkeletonRows·SkeletonCards（骨架）接入 KB 文档、Agent 列表、审批中心三页
- **设计系统**：ECharts Apple 主题完整定义（亮暗成对构建器/八色系/玻璃 tooltip/四轴同构/系列级默认）；Tooltip 基元（锚点定位/Esc/防抖显隐）
- **版本基建**：设置→资料→关于卡（应用版本/构建时间/Web·桌面运行环境对照）；CHANGELOG 与 git tag 制度（本文件）

### 变更

- join 受邀提示条文案 `text-wrap: balance` 消除孤字换行
- 终端白名单提示行文案缩句 + 暗色对比提亮一档（text-label-2）

### 修复

- 终端输入行占位符右缘裁字（视觉验收否决项）
- expanded 集合种子未渲染根路径的死状态（OCR 评审发现）
- jsdom 无 scrollIntoView 导致终端页签测试崩溃（`?.()` 可选调用）

## [0.1.0-m1] - 2026-09-28

### 新增

- M1 里程碑基线：全部 15+ 页面、MSW 契约 mock（api/01+02）、design-system 六层样式库（tokens/glass/elements/patterns/motion/utilities）、认证（含 2FA）、SSE 流式对话、证据抽屉、群聊编排、S8 收口（路由级懒加载 + manualChunks 六类分包 + 视口矩阵）
- 桌面端 Electron 壳 v1（FE-ADR-FE9）：oa:// 自定义协议、1440×900、contextIsolation 沙箱、托盘、preload appInfo 桥
- 品牌资产：本体立方体三面渐变 Logo（登录卡/侧栏/favicon/桌面 icon）+ 蓝族纹理工具类
- 圆角/阴影/字号令牌桥接（r-ctl 10px/r-panel 14px/r-card 18px；字阶六阶归一 740 处）
