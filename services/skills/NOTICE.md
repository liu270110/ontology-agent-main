# NOTICE — services/skills 中收编自上游的技能

## 来源

- **来源仓**：[hermes-agent-main](https://github.com/NousResearch/hermes-agent)（Nous Research，**MIT License**）
- **上游本地快照**：`ods-project/hermes-agent-main`（主仓内只读）
- **上游路径**：`skills/<域>/<name>/`（整目录复制）
- **收录日期**：2026-10-04
- **收编方式**：direct（整目录复制）；正文含 hermes 专属操作指令的已改写为通用写法，frontmatter **全部字段原样保留**（`metadata.hermes` 作溯源）

## 许可

上游 LICENSE（仓库根）原文关键行：

```
MIT License

Copyright (c) 2025 Nous Research
```

注：docx / pdf / xlsx 三个技能目录内自带 `LICENSE` 文件，其版权行为
`Copyright (c) 2026 Nous Research`（上游收录 Anthropic 风格文档技能时的标注），
均已随目录原样保留。MIT 全文见上游 LICENSE 或各技能目录内 LICENSE。

## 收录清单（12 个）

| 技能 | 上游路径 | 正文改写说明 |
| ---- | ---- | ---- |
| systematic-debugging | `skills/software-development/systematic-debugging` | `read_file`/`search_files`/`terminal` 等工具名 → ripgrep/终端等通用写法；"Hermes Agent Integration" 节 → "Tooling Integration (generic)"，`delegate_task` → 通用子代理委派 |
| test-driven-development | `skills/software-development/test-driven-development` | 同上，`terminal(...)` 调用 → 普通 bash；`delegate_task` → 通用委派简报 |
| codebase-inspection | `skills/software-development/codebase-inspection` | 无改写（通用 pygount 用法） |
| requesting-code-review | `skills/software-development/requesting-code-review` | `hermes chat -q` 一次性运行表述 → 通用 one-shot/CI；`delegate_task` 审稿/修复 prompt → 通用 text 简报 |
| simplify-code | `skills/software-development/simplify-code` | `delegate_task` batch mode / `delegation.max_concurrent_children` / toolsets → 通用并行子代理表述 |
| spike | `skills/software-development/spike` | `web_search`/`web_extract`/`terminal`/`write_file` → 通用研究工具与 bash；GSD 安装命令去 `--hermes` 旗标（Attribution 保留） |
| python-debugpy | `skills/software-development/python-debugpy` | Recipe 3 去 `scripts/run_tests.sh` 包装器表述；Recipe 5 Setup 的 Hermes PM 工作流 → 通用 debug venv；"Debugging Hermes-specific Processes" → "Debugging Long-Running Processes"；launch.json / scratch 路径 / Pitfall 1、8 通用化 |
| docx | `skills/productivity/docx` | 正文 `write_file`/`patch` → 通用文件工具表述；scripts 已过本仓 ruff |
| pdf | `skills/productivity/pdf` | `vision_analyze` → "image-analysis tool"；`write_file`/`read_file` → 通用表述；scripts 已过本仓 ruff |
| xlsx | `skills/productivity/xlsx` | `write_file`/`read_file` → 通用表述；scripts 已过本仓 ruff |
| weekly-review-planning | `skills/productivity/weekly-review-planning` | 正文 google-workspace / obsidian / notion / email-inbox-triage 连接器引用与 Automation Blueprint 表述 → 通用系统表述（frontmatter `related_skills` 按裁决保留作溯源） |
| arxiv | `skills/research/arxiv`（补选，research 域通用项） | `web_extract(...)` 调用 → 通用网页抓取表述；`ocr-and-documents` 技能引用 → 本仓 `pdf` 技能 |

## 排除清单（S1 裁决内未收录）

- **hermes 专属**：dogfood、hermes-agent-skill-authoring、inspecting-hermes-desktop-dom、node-inspect-debugger
- **强绑第三方账号/服务**：airtable、notion、google-workspace、box、teams-meeting-pipeline 等 productivity/research 其余连接器类
- **含未改写即不可通用的正文**（本批未收）：grounded-citations（脚本依赖 `_hermes_home.py`）、obsidian（绑 Obsidian vault + `HERMES_HOME`）

## 门禁记录（2026-10-04）

- `python -m ruff check services/skills services/devtools/hermes` → All checks passed
- 收编 py 全部 `py_compile` → 40 文件 0 失败（含 skills scripts/tests 与 tools）
- 12 个 SKILL.md：frontmatter PyYAML 可解析、name 与目录一致、description 非空、SKILL.md 内相对引用文件全部存在
