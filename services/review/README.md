# review（审核工作流，横切）

候选非成品门禁统一入口；review_tickets 六态（08 §4）。M2.2 填充候选进审链路。

- `business/candidates.py`：ReviewTicketService = review_tickets 写路径服务（M2.2 已落）。
  跨模块经 `services/platform/ports/review_port.py` 的 CandidateReviewPort 注入（review.data
  模块私有，pyproject 契约六），绑定在组合根 gateway lifespan（运行期装配，见 app.py 注释）；
  首个调用方 = kb 抽取流水线 extract/validate 步（services/kb/business/kb_extraction.py）。

- `business/candidates.py`（M5 扩展）：+ get_open_ticket/get_ticket/mark_published/merge_payload/
  approve/reject 与 **ReviewApprovalService**（治理三档审批链执行位，08 §2.4；档位读取经
  `data/governance.py` PgGovernanceTierReader 走 tenants.settings）+ 组合根工厂
  `build_review_approval`（契约六唯一豁免边 gateway.app→candidates）。
- `domain/approval_chain.py`：**三档判定单一收敛点**——parse_governance_tier / required_signatures /
  resolve_decision（solo 自批留痕、team 禁自批、enterprise 双负责人四眼），禁散写。
- 首个调用方：插件上架 target_type=plugin_listing（services/plugin/business/lifecycle.py 编排）。
