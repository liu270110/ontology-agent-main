---
name: dual-remote-sync
description: 双远程推送与核验技能。需要把 develop/master 推送到 Gitee(origin)与 GitHub(github)双远程、或核验远端真实位置时使用。含通道选择、强制 SSH 反向规则、跟踪引用假信号陷阱与积压分块推送法。
---

# dual-remote-sync:双远程推送与核验

本仓双远程:origin=Gitee(开发主仓,SSH)、github=GitHub(备份仓,HTTPS)。两仓通道状况不同,**推送与核验都必须按本技能口径**。

## 通道矩阵(2026-10-04 实测)

| 通道 | 状态 | 用法 |
| --- | --- | --- |
| Gitee 推送(SSH/HTTPS) | **可用**(2026-10-04 恢复,曾长期 RST 故障) | `git push origin develop` |
| GitHub HTTPS git 端点 | **被 RST**(fetch/push 均断) | 禁用直连 |
| GitHub 强制 SSH(反向 insteadOf) | **可用** | 见下 |

## GitHub 推送/核验命令(唯一可靠通道)

全局 git config 有 `url.https://github.com/.insteadOf = git@github.com:` 改写规则,会把 SSH 强转 HTTPS 导致必断。用**更长前缀的反向规则**在命令级压过它:

```
# 核验远端真实位置(推送前后各一次)
git -c url.git@github.com:liu270110/.insteadOf=https://github.com/liu270110/ \
  ls-remote git@github.com:liu270110/ontology-agent-main.git develop

# 推送(同样前缀)
git -c url.git@github.com:liu270110/.insteadOf=https://github.com/liu270110/ \
  push https://github.com/liu270110/ontology-agent-main.git develop
```

## 三条铁律

1. **核验只信 ls-remote,不信本地跟踪引用**:URL 直推成功**不会更新** `refs/remotes/github/*`;HTTPS fetch 又常被 RST 刷不动引用,于是 `git rev-list github/develop..develop` 会报出几百提交的**假落后信号**。判远端状态一律 ls-remote 实测。
2. **推送前脱敏**(docs/standards/02 §11):staged 内容过敏感串 grep 门禁(凭据/客户图号/内部标识/个人路径)零命中才推。
3. **master 晋级是窗口制**(docs/standards/02 §10):master 只收 develop 的 --no-ff 合入、五门全绿后由验收人开窗;平时只推 develop。

## 积压时分块推送(通道不稳时的兜底)

```
git rev-list --reverse origin/develop..develop | while read sha; do
  git push origin $sha:refs/heads/develop && break || sleep 5
done
```

注意:HEAD~N refspec 在 merge 密集拓扑上会报 does not match——必须用 rev-list 精确哈希;Git Bash 推 refs 时如遇路径转换问题加 `export MSYS_NO_PATHCONV=1`。

## 边界

- 推送失败重试 2~3 次仍断→停手记录,不无限重试(历史上越重试越糟的教训);
- 改写已推送历史(force-push)共享分支**严禁任何会话自行操作**,升级用户裁决。
