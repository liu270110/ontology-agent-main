# deploy - 部署配套

> 定位：L7 基础层的部署配套（compose/初始化脚本/环境配置）。数据契约的权威在 [docs/architecture/06-数据层设计.md](../docs/architecture/06-数据层设计.md)，本文只管"怎么把东西跑起来"。

## 规划内容（M0 里程碑交付）

```
deploy/
├── docker-compose.yml      # 五存储 + 网关 + 前端 一键起
├── init-pg/                # PG 初始化：建库、roles.scopes 种子数据（角色矩阵见 08 篇 §2.2）
├── init-neo4j/             # Neo4j：插件（n10s）、约束与索引
├── init-milvus/            # Milvus：三个 collection 创建（kb_chunks/graph_communities/memory_embeddings）
├── init-minio/             # MinIO：四个 bucket（raw-docs/extracts/ontology-artifacts/plugin-packages）
└── .env.example            # 环境变量样例（OA_ 前缀，命名见 07 篇 §6）
```

## 服务与端口约定

| 服务 | 端口 | 说明 | 档位 |
| ---- | ---- | ---- | ---- |
| gateway (FastAPI) | 8000 | REST + SSE；OpenAPI 文档 /docs | 双档 |
| frontend (Vite dev) | 5173 | 生产由 Nginx 托管静态资源并反代网关 | 双档 |
| postgres | 5432 | 业务权威库 | 双档 |
| redis | 6379 | 会话热数据/限流/队列 | 双档 |
| minio | 9000 (S3) / 9001 (Console) | 对象存储 | 双档 |
| neo4j | 7474 (HTTP) / 7687 (Bolt) | 图谱 + 本体物化（只读物化视图，权威在制品） | **full 档** |
| milvus | 19530 (+9091 metrics) | 向量检索 | **full 档** |

## 部署分档（2026-09-26 评审裁决，见锚点 §3.2）

| 档位 | 存储 | 语义层降级方式 | 适用 |
| ---- | ---- | ---- | ---- |
| **lite（默认）** | PG（含 pgvector）+ Redis + MinIO | 图遍历 → PG 递归 CTE；向量检索 → pgvector | 开发、演示、个人/小团队自托管 |
| **full** | PG + Neo4j + Milvus + MinIO + Redis 全量 | 无降级 | 生产、规模化 |

- compose 提供 `docker-compose.yml`（lite）与 `docker-compose.full.yml`（full 覆盖）；功能验收标准两档一致，性能档不同。
- ⚠ **Neo4j 社区版（GPLv3）不支持多 database 与 RBAC**：多租户只能属性级隔离；商用分发边界需法务确认——出现多库隔离/企业特性需求即触发企业版采购或迁 NebulaGraph/TuGraph 评估（锚点 §3.2、§7）。
- ⚠ Milvus 单机版也拉起 etcd/对象存储等依赖，是 lite 档不默认含它的原因之一；云/本地双形态（本地 compose vs 云托管 PG/Milvus）作为配置差异支持，不另建设计。

## 纪律

- compose 只做**开发与演示**环境；生产编排（K8s/Helm）在 M4 后按需引入。
- 初始化脚本必须幂等（重复执行不报错、不重复造数据）。
- 任何存储结构变更：先改 06 篇数据契约 → 再改 init 脚本/Alembic 迁移，顺序不可反。

## 双平台安装器与运维 CLI（2026-10-07 批次 A，docs/ops/04 §3/§5/§7）

| 产物 | 说明 |
| ---- | ---- |
| `install.sh` / `install.ps1`（仓库根） | 原生安装器八步（§3.1）：自检→uv 自举→`uv sync --frozen`→存储起底→迁移→配置档落位（三问合成 `.env`；已有则备份 `.env.bak-<ts>`+diff 提示，**绝不覆盖**）→`oadm doctor` 门禁→摘要。**两脚本语义逐条对齐**（步骤/参数/默认值/回退口径一致，验收单同一张）；`install.sh --skip-api` / `install.ps1 -SkipApi` = 混合开发态只装存储与 CLI（§3.3） |
| `bin/oadm` / `bin/oadm.cmd` | 运维 CLI 启动壳（经 `uv run` 进 venv）；命令面=ops/04 §5，批次 A：`doctor`（自检八项）/`status`（健康矩阵，`--output json` 供 CI）/`up --profile lite\|full\|full-cad`（幂等）/`down [--volumes]`（删卷须显式确认）；`migrate/config/capability/log` 为批次 B 占位 |
| `.env.lite` / `.env.full` / `.env.full-cad`（仓库根） | 三档配置模板（§4，对齐 standards/03 §3 矩阵默认能力集）。**只含既有 OA_\* 键**（唯一取配置入口 services/platform/config.py）；矩阵中尚未接线统一配置层的键（`OA_OCR_ENDPOINT`/`OA_VLM_*`/`OA_CAD_SERVICE_URL` 等）以**注释预留**，不作为生效配置 |
| `docker-compose.full.yml` | full/full-cad 全栈骨架（§3.2）：`api` 服务挂 services+前端预构建 dist（零 npm），`frontend`（nginx）可选注释态；compose profiles 随 `--profile full\|full-cad` 激活 |

- 安装器第 4 步复用探测：已有 healthy 的 onto-agent 存储栈则直接复用（幂等，§6 验收 3）；本机 Docker Hub 受限时可先 `sh deploy/up.sh` 完成镜像转存再重跑安装器（两路径幂等）。
- `up`/`down` 的 `-f` 组合：lite=base+local overlay；full/full-cad 追加本 full 骨架（`--profile` 激活 api）。`down` 覆盖全 profile（full 起的 api 一并停）。
- 行尾纪律：`bin/oadm`、`install.sh` 必须 LF，`bin/oadm.cmd`、`install.ps1` 必须 CRLF（根 `.gitattributes` 锁定）；`install.ps1` 必须 UTF-8 带 BOM（PS 5.1 中文注释）。
