---
name: wt-batch-close
description: worktree 批次收尾避坑技能。执行 wt finish 合入 develop、处理多会话并行冲突、主工作区脏文件共存时使用。含串行锁、同名文件挡合并、stash 仓级共享竞态等实战坑与处置。
---

# wt-batch-close:worktree 批次收尾

批次合入(wt finish)是全舰队共享的串行窄口,坑最多的一段。本技能是实战坑清单。

## 收尾检查单

1. **分支内全绿再 finish**:局部 pytest/ruff 过,git status 干净(worktree 有未提交内容会被 finish 门禁拒);
2. **finish 前自检合入面**:与主工作区未提交改动(他人)是否重叠路径——不重叠则无碍,**重叠=先沟通/让位,勿硬合**;
3. **finish 是串行锁**:报锁冲突=等锁重试,不是错误;期间 develop 可能被其他会话推进,finish 内部会 rebase 到最新。

## 实战坑与处置

| 坑 | 症状 | 处置 |
| --- | --- | --- |
| 同名未跟踪文件挡合并 | merge 报「local changes would be overwritten」但主工作区那批文件其实是**未跟踪**的(你分支新增了同名文件) | 主工作区原件**改名让位**(如 `<dir>` → `<dir>-local/`,内容不动),finish 后按需清理让位副本 |
| stash 仓级共享竞态 | stash 是仓级 ref,多会话同时 push/pop 会互相覆盖丢内容 | 跨会话尽量不用 stash 传内容;必须用时,带可识别消息标签,pop 前先 `git stash list` 核对 |
| finish 删 worktree | worktree 连同未提交内容一起消失 | finish 前把需保留的未提交内容先落 patch 或 stash 带标签 |
| 主工作区落后于 develop | 合入后主工作区代码不是最新 | develop 已被推进属正常;主工作区更新只允许快进/merge,**绝不 reset/checkout -- 覆盖**(有他人在途改动) |
| push 与工作区无关 | 有人担心 push 会动主工作区文件 | push 只动远端引用与 .git,安全;放心推 |

## 纪律回链

- 提交信息五要素 + 中文 conventional(commit-msg hook 强制);
- 批次关闭三件套(经验沉淀/能力沉淀评审/配置核对):docs/standards/03;
- 推送脱敏门禁:docs/standards/02 §11;master 晋级窗口:02 §10;
- 共享索引文件(README/AGENTS)只做**追加式**编辑。
