# services/ — 后端模块化单体（L2~L7）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[架构锚点](../docs/architecture/01-总体架构与分层.md)

## 1. 定位与边界

- 平台后端整体：**L2 网关 → L7 基础** 六层**模块化单体**（Modular Monolith）——一个 FastAPI 应用，内部按层严格分包，边界清晰，后续按边界拆服务（Agent 服务 / 语义服务 / MCP 网关优先拆出）。
- 依赖严格单向：**上层只能依赖下层，禁止逆向依赖**；唯一的「向上」是三处接口倒置（见 §3——2026-09-26 评审修复G：仓储/CapabilityProvider/ports 三处，原「两处」漏计技术能力端口）。
- ⚠ **目录名 `services` 为既有拼写错误，规范名是 `services`**。锚点 §7 已列为首次代码提交前的强制改名项（`services/ → services/`，同时 `docs/Agent/ → Agent/`、`docs/memory/ → memory/`、`docs/OntRAG/ → OntRAG/`）。当前目录尚无代码，改名零成本；一旦有 import 落地，每处 import 都在付成本。

## 2. 六层分包总表

| 层 | 目录 | 一句话职责 | 架构文档（细节权威） |
| ---- | ---- | ---- | ---- |
| L2 网关层 | [gateway/](./gateway/README.md) | REST/SSE 协议适配、鉴权、限流、审计、租户上下文、DTO 校验 | docs/architecture/02-网关层设计.md |
| L3 业务层 | [business/](./business/README.md) | 用例编排与事务边界：对话/任务/抽取/审核/记忆/插件生命周期 | docs/architecture/03-业务层设计.md |
| L4 领域层 | [domain/](./domain/README.md) | 聚合根/实体/领域事件/仓储接口，纯 Python 零框架依赖 | docs/architecture/04-领域层设计.md |
| L5 语义层 | [semantic/](./semantic/README.md) | 平台特色能力层：本体核心（TBox/推理/SHACL/版本）+ 知识库（GraphRAG） | docs/architecture/05-语义层设计.md |
| L6 数据层 | [data/](./data/README.md) | 仓储实现、ORM、Alembic 迁移、五存储客户端、Unit of Work | docs/architecture/06-数据层设计.md |
| L7 基础层 | [infra/](./infra/README.md) | 模型网关 LiteLLM、MCP 网关、插件运行时、消息、可观测、配置、安全 | docs/architecture/07-基础层设计.md |

> 分层职责与禁令全表见锚点 §2；内部分包树见锚点 §4。**02~07 篇是各层设计的细节权威**，各层 README 只写要点并链接过去，不重复展开。

## 3. 依赖规则

```text
gateway(L2) → business(L3) → { domain(L4), semantic(L5), infra(L7) }
semantic(L5) → { data(L6), infra(L7) }      data(L6) → infra(L7)
domain(L4) → （无，纯 Python）               infra(L7) → （仅外部系统）
```

三处**依赖倒置**（锚点 §3.4 例外 1~3，唯一允许的「向上」——2026-09-26 评审修复G：由两处更正为三处）：

1. **仓储接口**：L4 定义 `Protocol`，L6 实现——L4 永不 import L6；
2. **MCP 能力注册**：L7 定义 `CapabilityProvider` 协议，L3/L5 启动时把能力实现注册进网关注册表——L7 只认协议不认实现；
3. **技术能力端口**：L4 `domain/ports/` 定义 `ModelPort`/`ToolPort`/`KnowledgeRetriever` 等技术端口，L5/L7 提供实现（04 篇 §5：端口与领域服务分开，防「端口垃圾抽屉」）。

## 4. 公共约定（全层强制）

- **语言与工具**：Python ≥3.11（本地 3.13）；ruff（lint+format）+ mypy（strict 渐进）+ pytest；异步原生（asyncio）。
- **README 六段式**：每个代码目录的 `README.md` = 模块 spec，固定六段：职责与边界 / 目录结构规划 / 核心机制要点 / 接口契约 / 数据模型要点 / 开发指南与验收标准（锚点 §6.6）。
- **横切红线**（锚点 §6）：所有数据带 `tenant_id`；JWT + RBAC + scope；写操作与工具调用全审计；推理分级（规则/SHACL 优先，LLM 输出必过规则校验）；LLM 产物候选非成品、人工终审后生效；`trace_id` 贯穿 L2→L7。
- **配置与密钥**：一律经 `infra/config` 环境分层加载；密钥只在 `infra/security`，任何层不得自读环境变量里的密钥。

## 5. 快速开始

```bash
pip install -r requirements.txt          # 过渡期（后续切 uv）
docker compose -f deploy/docker-compose.yml up -d   # PG/Neo4j/Milvus/MinIO/Redis 五存储
alembic -c services/data/migrations/alembic.ini upgrade head
uvicorn services.gateway.app:create_app --factory --reload --port 8000
```

> `main.py` 为临时入口，M1 起改为上述 uvicorn 方式启动网关应用工厂。

## 6. 待办与开放问题

- [ ] ⚠ 首次代码提交前完成 `services/ → services/` 及三处 docs 目录改名（见 §1）。
- [x] ~~docs/architecture/02~07 六篇分层设计文档编写~~（已定稿：02/08 篇 v0.2（含 2026-09-26 评审修订），其余 v0.1——2026-09-26 评审修复G 更正「当前缺位」滞后表述）。
- [ ] 引入 import-linter 在 CI 强制分层依赖规则（含三处倒置白名单——2026-09-26 评审修复G 同步更正）。
- [ ] CI（Gitee Go）Python 3.9 → 3.11+（锚点待办）。
