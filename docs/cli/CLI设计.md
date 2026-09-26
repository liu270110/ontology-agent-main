# CLI（onto）- 设计

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[架构锚点：01-总体架构与分层](../architecture/01-总体架构与分层.md)、[研究整理/00-平台总体架构](../研究整理/00-平台总体架构.md)、[架构设计/05-对外服务协议与插件边界](../架构设计/05-对外服务协议与插件边界.md)（2026-09-26 旧稿合入）

---

## 1. 定位

- 开发者 / 运维 / CI 的**统一入口**；只调网关 REST API（`/api/v1/...`），**不在本机直连 PG / Neo4j / Milvus 等存储**（分层纪律：任何客户端不得绕过网关，架构锚点 §2）；
- 与未来的 Python SDK **共用同一 client 包**（`src/onto/client/`，成熟后可独立拆包发版 `onto-sdk`）；
- 代码目录：仓库根 `cli/`（架构锚点 §4），工程 spec 见 [cli/README.md](../../cli/README.md)。

## 2. 技术选型

| 项 | 选型 | 理由 |
| ---- | ---- | ---- |
| 语言 | Python ≥ 3.11 | 与后端同栈，client 代码可复用 |
| 命令框架 | **Typer** | 类型注解驱动、自动 help、子命令树友好 |
| 终端输出 | **Rich** | 表格 / 进度条 / 彩色 / Markdown 渲染 |
| HTTP | httpx | 超时控制、连接池、异常类型清晰 |
| 打包 | pyproject，包名 **`onto`**，入口 `onto = onto.cli:app` | pip 安装即用 |

## 3. 命令树总表

| 命令 | 用途 | 对应 API |
| ---- | ---- | ---- |
| `onto auth login / logout` | 登录（token）/ 登出 | `POST /api/v1/auth/login`、DELETE session |
| `onto kb upload FILE` | 上传文档进抽取流水线 | `POST /api/v1/kb/documents` |
| `onto kb status DOC_ID` | 查看抽取/审核进度 | `GET /api/v1/kb/documents/{id}` |
| `onto kb review list / approve / reject` | 人工终审候选实例 | `GET/POST /api/v1/reviews...` |
| `onto onto lint DIR` | 本体建模 lint（IRI/命名/闭环约束） | `POST /api/v1/ontology/lint` |
| `onto onto validate` | SHACL + 一致性校验 | `POST /api/v1/ontology/validate`、`/reason` |
| `onto onto push DIR` | 推送建模产物（生成 changeset 走评审） | `POST /api/v1/ontologies/{id}/changesets` |
| `onto onto diff A B` / `onto onto log` | 版本对比 / 变更历史 | `GET .../diff`、`GET .../versions` |
| `onto agent list / run` | 托管 agent 列表 / 运行任务 | `GET /api/v1/agents`、`POST /api/v1/agents/{id}/runs` |
| `onto session chat SESSION_ID` | 终端对话（SSE 事件流渲染） | `POST /api/v1/sessions/{id}/messages` |
| `onto memory search QUERY` | 检索多层记忆 | `POST /api/v1/memory/search` |
| `onto plugin search / publish / install` | 市场检索 / 发布 / 安装插件 | `GET/POST /api/v1/plugins...` |
| `onto mcp list / register` | 外部 MCP Server 清单 / 注册 | `GET/POST /api/v1/mcp/servers` |
| `onto admin tenant / user / usage` | 租户/用户管理、用量报表 | `/api/v1/admin/...` |

示例输出（`onto onto validate --ontology supply`）：

```text
✔ SHACL 校验通过（4,821 三元组，387ms）
✘ 一致性检验发现 1 处矛盾（owlrl，2.1s）
  └ #C042  Device 与 Consumable 声明互斥，但等同公理使其相交
退出码：2（校验失败）
```

示例输出（`onto kb review list --status pending`）：

```text
ID        文档                类型      候选数  提交人    时间
RV-0102   电池标准V3.pdf      实例      47      wang      2026-09-25 14:22
RV-0103   供应商流程图.png    关系      12      li        2026-09-25 16:05
```

### 3.1 逐组参数明细

| 组 | 命令 | 关键参数 |
| ---- | ---- | ---- |
| auth | `onto auth login` | `--token`（CI 直接传）、`--endpoint`、`--tenant`；`logout` 无参 |
| kb | `upload` | `FILE`、`--kb-id`、`--wait`（阻塞到预处理完成）；`status` 取 `DOC_ID` |
| kb review | `list` | `--status pending\|approved\|rejected`、`--kb-id`；`approve/reject` 取 `REVIEW_ID` + `--comment` |
| onto | `lint` | `DIR`（本地建模产物目录，Turtle/JSON-LD）；`validate` 取 `--ontology` + `--wait`；`push` 取 `DIR` + `--changeset TITLE`；`diff` 取 `A B`（版本号或 changeset id）；`log` 取 `--ontology` + `--limit` |
| agent | `run` | `AGENT_ID`、`--task`、`--input FILE`、`--follow`（跟随事件流）；`list` 取 `--tenant` |
| session | `chat` | `SESSION_ID`、`--new`（新建会话）、`--agent` |
| memory | `search` | `QUERY`、`--level session\|user\|org\|knowledge`、`--top-k` |
| plugin | `search` | `KEYWORD`、`--category`；`publish` 取 `DIR`（含 server.json）、`--skip-gates`（仅 admin）；`install` 取 `PLUGIN_SLUG` + `--version` + `--grant-scopes` |
| mcp | `register` | `--transport stdio\|http`、`--url`/`--command`、`--name`；`list` 取 `--status` |
| admin | `tenant/user/usage` | 子动作（create/list/disable）；`usage` 取 `--from --to --group-by tool\|agent\|tenant` |

示例输出（`onto onto diff 3 4 --ontology supply`）：

```text
变更单 CS-021（base v3 → v4）  状态: in_review
+ ADD    类      #Supplier（subclassOf #Party）
+ ADD    规则    R014 [engine] 事件 #OrderDelayed → 行动 #EscalateOrder
- REMOVE 属性    #hasOldStatus（标记：影响实例 1,204 条）
~ MODIFY 公理    R009 [shacl] 收紧 #orderNo 格式 sh:pattern
```

## 4. 配置与凭据

`~/.onto/config.yaml`：

```yaml
endpoint: https://onto.example.cn/api/v1
tenant: t1
output: table        # table | json | yaml
timeout: 30          # 秒
```

- **环境变量覆盖**（优先级高于配置文件，CI 场景不落盘）：`ONTO_ENDPOINT` / `ONTO_TOKEN` / `ONTO_TENANT` / `ONTO_OUTPUT`；
- **token 存储**：交互式登录后写入 `~/.onto/credentials`（权限 0600）；系统有 keyring 库时优先存系统钥匙串；`--token` 参数仅推荐 CI 使用。

### 4.1 对外通道矩阵与 CLI 默认配置对齐（2026-09-26 旧稿合入）

#### 三出一进：CLI 在平台对外协议矩阵中的位置

平台对外协议关系 = **三出、一进**：三条出口（REST API / MCP / `onto` CLI——同一业务层用例的三张协议皮）+ 一条入口（插件）。选通道先查矩阵：

| 消费方 | 首选通道 | 备选 | 不提供 |
| ---- | ---- | ---- | ---- |
| 前端 SPA | REST `/api/v1` + SSE（AG-UI 事件流） | —— | —— |
| 第三方业务系统（服务端集成） | REST（API Key） | MCP（对方是 agent/LLM 系统） | CLI |
| 外部 Agent / MCP Client | MCP `/mcp` | REST | SSE 直连 |
| 开发者终端 / CI 流水线 | **`onto` CLI** | REST | SSE |
| 平台内托管 agent（插槽） | 工具注册中心直连（内部优化路径） | MCP 自调用（互操作测试） | —— |
| 想给平台加能力的外部开发者 | **插件市场**（一入口） | —— | 改内核、直连数据库 |

对 CLI 的两条矩阵铁律：**单一事实源**——三出口暴露同一业务层用例（工具注册中心统一登记元数据与语义标注），不允许"CLI 能做而 API 不能做"的能力差；新增能力先落业务层 + 注册中心，三个出口自动获得。**CLI 是 REST 的薄壳**（§1 既定）。另：MCP 不暴露管理面——IAM、审批、插件上下架等管理能力只走 REST，`onto` 命令树即映射此面。

#### CLI 通道默认配置对齐（endpoint / token / 输出格式）

旧稿 CLI 协议默认值对齐到现行 §4 配置节（旧稿 `oa` / `OA_*` → 现行 `onto` / `ONTO_*`，不引入第二套命名）：

| 旧稿默认项 | 现行落位 |
| ---- | ---- |
| `OA_ENDPOINT` / 配置文件 endpoint | `ONTO_ENDPOINT` / `~/.onto/config.yaml` `endpoint`（§4 已覆盖） |
| `OA_API_KEY` / 凭据不明文落日志 | `ONTO_TOKEN` / `~/.onto/credentials`（0600，keyring 优先；§4 已覆盖） |
| `OA_OUTPUT`：默认人类可读表格，`--output json` 机器消费强制结构化 + UTF-8 | `output: table` 默认 / `ONTO_OUTPUT` / `--output json\|yaml`（§4/§6 已覆盖；**补强采认：json/yaml 模式强制 UTF-8 输出**，保障 agent/CI 消费确定性） |

两项采认增量（现行 §4 未含）：**profile 多环境**——配置文件支持多 profile（多套 endpoint/tenant 凭据切换），多环境不靠多份文件；**启动版本协商**——CLI 启动时探测服务端 `/api/v1` 版本，不兼容时提示升级而非抛报错堆栈。

#### CLI-Anything（cli-harness 三轨插件）默认配置

CLI-Anything 为首个**官方认证外置插件**（cli-harness 三轨：平台内置通用执行器 `cli-harness-runner` 把 Click 系 CLI + SKILL.md 包装为工具），默认配置随插件捆绑发布、租户可改，经 `onto plugin install / uninstall` 管理生命周期：

| 配置项 | 默认值 | 说明 |
| ---- | ---- | ---- |
| `install_source` | PyPI `cli-anything-hub`（可配私有镜像） | 安装源白名单 |
| `execution` | 插件沙箱独立容器；CPU 2 核 / 内存 2GB / 单命令超时 120s / 并发 4 | 资源配额 |
| `network` | 默认**全部拒绝**；放行 PyPI 镜像 + 各 harness 登记的必需端点（逐 CLI 声明） | 出网白名单，宁严勿松 |
| `output` | 强制 JSON 输出（等价 `--output json`）；超 64KB 截断并附截断标记 | 与 §6 输出规范同构 |
| `filesystem` | 仅 `/workspace/<harness>` 可写；禁符号链接逃逸 | 路径安全 |
| `approval_policy` | 未分类命令**一律按"写类"走审批中心二次确认**；管理员逐 harness 标 read/write/dangerous，read 类免审 | CLI-harness 无 annotations 可依，默认从严 |
| `skills_sync` | 开启：SKILL.md 自动同步 Skills 库、按 agent 分发 | 程序记忆落点 |
| `preinstalled` | **空**——不预装任何 harness，按需 `cli-hub install` | 最小够用 |
| `desktop_targets` | 禁用（Blender/GIMP 等需宿主桌面软件的 harness 标记不可用；v1 只放开 headless/API 类） | 与服务器部署形态匹配 |
| `generator` | 构建容器出网仅限目标源码仓库 + PyPI；生成物必经候选审核 | CLI 化生产线的门禁 |

## 5. CI 集成（Gitee Go）

本体仓库变更触发流水线：lint / validate 不通过即卡流水线（退出码非 0）；`push` 只生成 changeset 进入人工评审——**CI 不自动 publish**（"候选非成品"横切约束，架构锚点 §6-5）。

```yaml
# .workflow/master.yml 片段
main:
  steps:
    - name: ontology_lint_and_push
      jobType: shell
      steps:
        - run: |
            pip install onto
            onto auth login --token "$ONTO_TOKEN"
            onto onto lint ./ontology/
            onto onto push ./ontology/ --changeset "ci-${GITEE_BRANCH}-${GITEE_PIPELINE_NUMBER}"
            onto onto validate --ontology supply --wait
```

## 6. 输出规范与退出码

- 全局选项：`--output table|json|yaml`（json/yaml 模式禁用彩色与装饰，可被 jq / yq 消费）、`--no-color`、`--timeout`；
- 进度类命令（upload / run / chat）在非 TTY 环境自动退化为逐行日志。

| 退出码 | 含义 |
| ---- | ---- |
| 0 | 成功 |
| 1 | 通用错误（参数错误 / 内部异常） |
| 2 | 校验失败（lint / validate 不通过，含网关 422） |
| 3 | 网络错误（连不上网关 / 超时） |
| 4 | 鉴权失败（token 无效或过期，提示重新 login） |
| 5 | 资源不存在（404 映射） |
| 6 | 权限不足（403 映射） |

补充约定：

- 列表类命令统一支持 `--filter key=value` 过滤与 `--limit N`；
- `--output json` 顶层结构固定为 `{"data": ..., "meta": {"count": 0, "elapsed_ms": 0}}`，字段只增不改（未来 SDK 依赖其稳定性）；
- 管道友好：非 TTY 下自动关闭彩色与进度条动画。

## 7. 验收标准

- [ ] §3 命令树全部命令可用且 `--help` 完整（命令 = 帮助 = API 三者一致）；
- [ ] `--output json` 输出可被 `jq` 解析；退出码严格按 §6 约定（CI 可靠退出）；
- [ ] Gitee Go 示例流水线端到端跑通：lint 失败能卡住流水线，push 生成的 changeset 出现在评审队列；
- [ ] 代码审查 + 抓包确认无任何直连存储的路径；
- [ ] token 不出现在进程列表参数与日志中（安全用例）。

## 8. 待办与开放问题

- [ ] `onto agent run` / `onto session chat` 的 SSE 消费与 Typer/Rich 流式渲染体验 PoC
- [ ] keyring 在国产化桌面环境（UOS / 麒麟）的可用性验证，失败时降级 0600 文件
- [ ] `onto plugin publish` 的打包格式定稿（tar.gz + 签名，对齐 docs/Skills §3）
- [ ] 与 Python SDK 的拆包时机（独立 `onto-sdk` 发版条件：第二个消费方出现）
- [ ] profile 多环境与启动版本协商的实现排期（2026-09-26 旧稿合入）
