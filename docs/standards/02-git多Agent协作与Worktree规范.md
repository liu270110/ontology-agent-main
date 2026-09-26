# 02 - Git 多 Agent 协作与 Worktree 规范

> 状态：v1.0 | 日期：2026-09-27 | 上游依据：根 README「分支策略」、[architecture/08-横切关注点与工程规范 §8](../architecture/08-横切关注点与工程规范.md)（分支模型战略层）、[01-编码规范](./01-编码规范.md)（Git 规范细则）
>
> 一句话：**主工作区 = develop 集成区（禁直提）；每个任务/agent 在独立 worktree + 独立 feature 分支工作；串行锁 + `--no-ff` 合入；hooks 强制门禁。** 这就是传统大团队"功能分支 + 受保护集成分支 + 评审合入"模式的本地多 agent 版。

---

## 1. 为什么（要解决的问题）

多 agent（多会话）在同一工作区并行开发已发生的事故（全部真实记录）：

| 事故 | 根因 | 本方案的消除机制 |
| ---- | ---- | ---- |
| README/AGENTS.md 并发编辑报 "file modified since read"，互改丢失 | 同一工作区同一文件 | worktree 物理隔离，各自工作副本 |
| A 会话的暂存区被 B 会话的 commit 扫走（"混合署名"） | 暂存区是**全仓库一份**的共享资源 | 每工作区独立 index，暂存区不再共享 |
| 编号撞号（04/05 被两个会话同时占用） | 先占后编无注册点 | 分支内改，合并时统一裁决（§6） |
| develop 被并行会话竞态直提（先后脚 commit 交错） | develop 无保护 | hooks 禁直提 + 串行集成锁（§5） |
| 未完成合并/半截产物阻塞他人 | 工作区状态互相可见 | 故障半径=单 worktree |

## 2. 分支模型与工作区布局

```
master（发布保护：禁直提，只接受 develop 合入；对应远端保护分支）
  └── develop（集成分支：只接受 --no-ff 合并；主工作区检出）
        ├── feature/{域}-{简述}   ← agent A（worktree: ../wt-{slug}）
        ├── feature/{域}-{简述}   ← agent B（worktree: ../wt-{slug}）
        └── ...
```

| 工作区 | 检出分支 | 用途 | 谁可以提交 |
| ---- | ---- | ---- | ---- |
| 主工作区（仓库原目录） | develop | **只做集成交接**，不开发 | 仅合并提交（hooks 放行 `MERGE_HEAD` 场景） |
| `../wt-{slug}/` | feature/{slug} | 一个任务/一个 agent 的全部开发 | 该任务 freely |

- worktree 固定放在**仓库同级目录**（`../wt-{slug}/`）：路径短、天然在版本库外、无需 .gitignore；
- 每个 worktree 有**独立的** HEAD / index / 暂存区——两个 agent 永远不会互相扫走对方的东西；
- 主工作区里他人的**未暂存在途修改**不阻塞合入（合并只受重叠路径影响，冲突会安全 abort）。

## 3. 命令手册（`tools/git/wt`）

```sh
sh tools/git/wt new <slug> [base]   # 建 worktree+分支（slug 建议含域前缀，如 kb-conflict-ui）
sh tools/git/wt list                # 看全部 worktree
sh tools/git/wt sync                # feature 分支 rebase 到最新 develop
sh tools/git/wt finish [--push] [--keep]   # 串行合入 develop，默认清 worktree/分支
sh tools/git/wt prune               # 清失效记录
```

标准生命周期：

```
wt new kb-conflict-ui                    # 1. 开分支（从最新 develop）
  … 开发、小步提交 …                     # 2. 全程在 worktree 内
wt sync                                  # 3.（可选）中途对齐 develop
wt finish --push                         # 4. rebase→串行锁→--no-ff 合入→清理→推送
```

## 4. 提交纪律

1. **中文 conventional**（hooks 的 commit-msg 强制）：`<type>(<scope>)?: 描述`，type ∈ feat|fix|docs|style|refactor|perf|test|build|ci|chore|assets；`Merge *` / `Revert *` 豁免；
2. **小步提交**：一个逻辑改动一个 commit；禁止 `--no-verify` 绕过门禁（紧急例外须用户明示并在提交信息注明原因）；
3. **scope 限定**：一个分支只做一个任务；不夹带无关文件（发现顺手该改的 → 另开分支）；
4. **大文件**：Gitee HTTPS 推 >1MB 单文件会被重置——大文件拆分提交或走 SSH（见 AGENTS.md 已知注意事项）。

## 5. 集成规则（develop 只进不改）

`wt finish` 的固定流程与保证：

1. **前置自检**：feature 分支、工作区/暂存区干净；
2. **串行锁**：`.git/wt-integrate.lock`（mkdir 原子创建；持有 >600s 判残留接管）——**同一时刻只有一个 agent 在动 develop**；
3. **rebase 对齐**：优先 `origin/develop`（离线回退本地）；冲突 → abort 回本分支解决，**不在 develop 上解冲突**；
4. **`--no-ff` 合并**：主工作区执行，保留集成痕与分支边界；冲突（共享文件改动重叠）→ abort，回本 worktree `wt sync` 解决后重试——develop 上永远不会留下半截状态；
5. **清理**：删 worktree 与分支（历史已在合并提交里）；`--keep` 调试时保留；
6. **推送**：`--push` 时 `git push origin develop`。

升级路径（远端保护就绪后）：feature 分支推 Gitee 开 Pull Request、经评审合入——流程语义与本节完全一致，届时 `wt finish` 退化为 `wt push-pr`。

## 6. 共享文件协作规则（冲突的最后阵地）

索引类文件（根 README、AGENTS.md、docs/架构设计/README.md、各目录 README）是唯一会经常合并冲突的文件，纪律：

1. **追加式编辑**：往表格/清单**末尾追加行**，不重排他人行、不改他人行文案（改他人文案 = 提交用户裁决）；
2. **晚改早合**：共享文件改动放到分支**最后一个 commit**，分支做完立刻 finish——缩短暴露窗口；
3. **编号注册**：新文档编号 = 合入时 README 索引最大值 +1；分支内先占号、合并冲突时后到者让位重编；
4. **冲突机械解**：两边都是追加行 → 两行都保留；同行互斥 → 按"先到先得 + 提交用户裁决"，不得静默覆盖。

## 7. 与评审、CI 的衔接

- **代码分支**：`wt finish` 前建议先跑 `ocr review`（OpenCodeReview，见 skills/code-review-ocr）与 `ruff check`，问题清零再合入；
- **CI**：Gitee Go 流水线挂在 develop 推送上（`.workflow/develop-pipeline.yml`），合入后由 CI 兜底（lint/测试/契约）；
- **文档分支**：纯文档分支可免 ocr，但须过 §6 共享文件纪律。

## 8. 常见问题

| 问题 | 处置 |
| ---- | ---- |
| 误在 develop 直接提交被 hook 拒绝 | 正常——`wt new` 开分支，把改动带到分支上（改动还在暂存区/工作区，切走前 `git stash -u`，在分支 `stash pop`） |
| worktree 目录残留在磁盘但 `wt list` 没有 | `wt prune`；目录手动删 |
| finish 提示"另一 agent 正在合入" | 等它完成重跑；锁残留 >600s 会自动接管 |
| 想同时开多个任务 | 每个 `wt new` 一次——worktree 数量不限，磁盘各自独立 |
| 在 worktree 里切到 develop | 禁止——develop 已被主工作区检出，git 会拒绝；这正是保护 |
| 主工作区提示"暂存区非空"无法 finish | 他人的在途批，等其提交；这是暂存区共享时代的旧问题在主工作区的最后残留 |

## 9. 首次启用（已执行，备查）

```sh
git commit    # 基线：清空主工作区遗留暂存批（6ba3e36）
git worktree add -b feature/git-workflow ../wt-git-workflow   # 本规范即在该 worktree 内交付
git config core.hooksPath tools/git/hooks                     # 门禁启用（版本化 hooks，全 worktree 生效）
sh tools/git/wt finish --push                                 # 自举合入
```
