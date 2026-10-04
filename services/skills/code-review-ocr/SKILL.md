---
name: code-review-ocr
description: 用 OpenCodeReview（ocr CLI）做 AI 行级代码评审。提交/PR 前、或用户要求评审代码时使用。含标准命令、范围排除纪律、代理委托模式（无 LLM key 环境）与结果处置规则。
---

# code-review-ocr：OCR 代码评审

本仓库的 AI 代码评审统一走 [OpenCodeReview](https://github.com/alibaba/open-code-review)（全局 `ocr` CLI）。LLM 已配置在 `~/.opencodereview/config.json`（provider=deepseek），无需每次传 key。

## 标准命令

```bash
# 提交前：评审工作区改动（staged + unstaged + untracked），agent 摘要输出
ocr review --audience agent

# 分支评审：相对 master 的全部差异
ocr review --from master --audience agent

# 单个 commit 评审
ocr review -c <hash> --audience agent

# 只看会评哪些文件，不花 token
ocr review --preview
```

## 范围纪律（必带排除项）

工作区常驻研究/归档/前端大块改动，评审目标之外的一律排除，控制 token 与噪音：

```bash
ocr review --audience agent \
  --exclude 'resrch-pj/**,docs/Dsec/**,docs/archive/**'
```

- 纯 Markdown 文档（docs/**）OCR 自动按 unsupported_ext 跳过，无需手动排除；
- 评审前先 `--preview` 确认文件清单，避免误扫大目录。

## 结果处置

1. 评审完成输出 `Session: <id>`，可用 `ocr session` 系列命令回看历史；
2. **high/medium 发现先报告用户**，附文件与行号；发现属于他人未提交产物时只报告不代改（多会话纪律）；
3. 修复后可 `ocr review --resume <session-id>` 增量复审；
4. 输出格式：给人看用默认 text；给程序吃用 `--format json`（SARIF 用于 CI 安全面板）。

## 委托模式（无 LLM key 环境）

新环境未配 key 时，OCR 可退化为规则分发器，由宿主 agent（我）自己执行评审：

```bash
ocr delegate preview          # 列可评审文件与分组
ocr delegate rule <files...>  # 输出按内容分组的评审规则，据此人工评审
```

## 配置速查

```bash
ocr llm test          # 验证 LLM 连通
ocr config set provider deepseek   # 换 provider
ocr config set model <name>
```
