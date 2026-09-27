# plugin（插件市场+工具集+技能，模块 6）

M5 填充（权威=docs/Skills）。

## M5-1 最小版落地面（2026-09-27，迁移 a1b2c3d4e5f6）

- `domain/model/plugin.py`：Plugin 聚合六态状态机（draft→submitted→in_review→published→suspended→deprecated，
  负向测试锚点）+ PluginVersion 四态（DDL 逐字）+ ToolBinding；六态→plugins.status 三值投影
  单一收敛点（to_storage_status/from_storage_status——submitted/in_review 由 open 工单承载，suspended 为运行期叠层）。
- `domain/model/manifest.py`：server.json 值对象 + 门禁 1（schema 校验）服务端实跑；2~6 关以 `gates_pending` 如实标注未建。
- `business/lifecycle.py`：PluginMarketService（登记/提交进审/三档审批决策+发布联动/安装/启停）。
- `runtime/`：进程内注册表（load/start/stop/health_check）+ PluginCapabilityProvider（经
  platform.ports 协议注册进 mcp registry，组合根 gateway lifespan 装配）；沙箱供给=结构化协议
  PluginSandboxBackend（契约九禁 plugin→sandbox.runtime，真实后端注入需豁免边，见批次报告）。
- `api/plugins.py`：api/01 §5.6 八端点（列表/详情/登记/新版本/submit/install/enable/disable）。

## M5-1 遗留收口（2026-09-28，门禁 2~6 + 两级签名 + §5.6 剩余端点；零迁移）

- `business/gates.py`（新建）：门禁 2~6 清单级纯函数链 + `run_gates` 编排（门禁 1 包装引用
  manifest.py，本体不动）。scan_report 由 `gates_pending` 分期标记升级为**逐关真实结论**
  （gates.schema_v1/protocol/static_scan/poison/dependency/behavior_fixture，失败必附可修复
  findings）；submit 全链实跑，任何关失败退回 draft（4503），任何档位不可跳过（宪法 3）。
  模式集/黑名单/支持矩阵均为可维护常量；沙箱握手、SBOM 漏洞库、本体公理夹具生成器为
  清单级边界外的下一批（见批次报告遗留）。
- `platform/security.py`（增量）：Ed25519 两级签名（Skills §5.1）——开发者签（manifest+包摘要）
  → 平台签（发布者签名+清单）；`PluginSigner` 平台签名器（密钥=config
  `OA_PLATFORM_PLUGIN_SIGNING_KEY`，缺失 fail-closed）；验签顺序=先平台签后开发者签。
  `platform/config.py`：新增 `platform_plugin_signing_key`（开发期 dev key 生成见 config 注释）。
- `business/lifecycle.py`：submit 接门禁链；发布联动替换占位签为平台真签并回填
  `x-platform.signature`（上架态必填）；install 前两级验签（失败 4509 拒装）；密钥缺失发布
  拒绝（4510）；新增 update_metadata/deprecate（软删终态）/list_versions 用例。
- `api/plugins.py`：§5.6 剩余端点 PUT /plugins/{id}（plugin:write，200/404/4501）、
  DELETE /plugins/{id}（deprecated 软删，plugin:write）、GET /plugins/{id}/versions
  （plugin:read）；错误映射补 4509→403、4510→503。
- `data/repo_impl/plugin_repo.py`：save 增列 name（PUT 元数据）、save_version 增列 server_json
  （发布签回填持久化）。
