# iam（租户/用户/角色/APIKey/审计）

api/auth 三端点（login/refresh/logout）+ security 原语在 platform/security + data/orm(identity 5 表+audit_logs)。
api/invites 邀请链接五端点（架构设计/32，api/01 §5.8）+ api/users 用户管理 CRUD 四端点（B8-WA，api/01 §5.8）。
api/admin admin 域 9 组端点（2026-10-05 批，api/01 §5.8 admin 行 + §5.10 groups/permission-requests 预登记实装）：
audit-logs（列表/trace 展开/导出建任务）+ system-logs（audit+llm 双源）+ analytics/overview（p-analytics 轻量版）
+ groups + roles/matrix + models（渠道 CRUD/test/impact）+ tenants + api-keys + permission-requests（第六类工单联动）；
DTO=api/schemas/admin.py，聚合仓储=data/repo_impl/admin_repo.py，存储=data/orm admin 域 4 表
（user_groups/role_permission_matrix/model_channels/permission_requests，迁移 20261005_c5e9a1d3b7f5）。
