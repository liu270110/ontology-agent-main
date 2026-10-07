# cli/ —— 平台 CLI 工程代码目录

> 代码目录 README = 模块 spec（架构锚点 §6-6）。本目录是 CLI 的工程代码（当前为占位，按本 spec 开发）。

## 定位

开发者 / 运维 / CI 的统一入口。**只调网关 REST API**，不在本机直连 PG / Neo4j / Milvus / MinIO / Redis（架构锚点 §2 分层纪律）。与未来 Python SDK 共用 `src/onto/client/` 包。

## 技术栈

Python ≥ 3.11 + **Typer**（命令框架）+ **Rich**（表格/进度/彩色输出）+ httpx；包名 `onto`，入口 `onto = onto.cli:app`。

## 目录规划

```
cli/
├── pyproject.toml
├── src/onto/
│   ├── cli.py            # Typer 主应用与命令树装配
│   ├── client/           # 网关 REST 客户端（与未来 SDK 共用，可拆 onto-sdk）
│   ├── config.py         # ~/.onto/config.yaml + 环境变量覆盖（ONTO_ENDPOINT/ONTO_TOKEN/...）
│   ├── auth.py           # 登录 / token 管理（credentials 0600 或 keyring）
│   ├── output.py         # Rich 渲染 + --output table|json|yaml + 退出码约定
│   └── commands/         # auth / kb / onto / agent / session / memory / plugin / mcp / admin
└── tests/
```

## 关联设计文档

[docs/cli/CLI设计.md](../docs/cli/CLI设计.md) —— 命令树总表、参数与示例输出、配置与凭据、Gitee Go CI 集成示例、输出规范与退出码（0/1/2/3/4/5/6）。

## 验收标准

- 命令树全部命令可用且 `--help` 完整；`--output json` 可被 `jq` 解析；
- 退出码严格按设计文档 §6 约定（CI 依赖退出码卡流水线）；
- 无任何直连存储代码路径；token 不出现在进程参数与日志中。
