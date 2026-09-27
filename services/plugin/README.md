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
