"""记忆审计执行层已并入 data/repo_impl/fact_repo.py（2026-09-28 接管收口迁移指针）。

迁移原因：「memory.data 与 sandbox.data 模块私有」契约（pyproject 冻结）对
gateway/agent/mcp → memory.data 仅豁免 data.l1 与 data.repo_impl.fact_repo 两条消费边，
本模块作为新边无法被 api/memory.py 与 business/runtime.py 合法消费；原 query_memory_audit /
record_memory_promotion / promotion_exists / query_promotions / record_faithfulness_sample
（含 memory_audit_ready、MemoryAuditUnavailableError）全量移入
services.memory.data.repo_impl.fact_repo「记忆审计执行层」段，函数签名与语义不变。
本文件保留为指针，禁新增逻辑。
"""
