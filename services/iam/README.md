# iam（租户/用户/角色/APIKey/审计）

api/auth 三端点（login/refresh/logout）+ security 原语在 platform/security + data/orm(identity 5 表+audit_logs)。
api/invites 邀请链接五端点（架构设计/32，api/01 §5.8）+ api/users 用户管理 CRUD 四端点（B8-WA，api/01 §5.8）。
api/admin admin 域 9 组端点（2026-10-05 批，api/01 §5.8 admin 行 + §5.10 groups/permission-requests 预登记实装）：
audit-logs（列表/trace 展开/导出建任务）+ system-logs（audit+llm 双源）+ analytics/overview（p-analytics 轻量版）
+ groups + roles/matrix + models（渠道 CRUD/test/impact）+ tenants + api-keys + permission-requests（第六类工单联动）；
DTO=api/schemas/admin.py，聚合仓储=data/repo_impl/admin_repo.py，存储=data/orm admin 域 4 表
（user_groups/role_permission_matrix/model_channels/permission_requests，迁移 20261005_c5e9a1d3b7f5）。
api/me me 域 11 端点（2026-10-05 批，api/01 §5.9 totp 行 + §5.13 me 行 + §5.15 ★ 行实装，28 篇账号自助域）：
preferences（GET/PUT，浅合并+display_name 兼写资料列，email 拒改）+ 设备会话（GET 列表 current=jti 比对/
单 revoke/DELETE /auth/sessions/all 三件收口=行吊销+jti 拉黑+refresh 全家水位）+ 导出任务（202 建 tasks 行
type=me_export+GET 轮询+在途 409+4102）+ totp 四端点（setup/enable/backup-codes/disable，stdlib RFC 6238）；
DTO=api/schemas/me.py，聚合仓储=data/repo_impl/me_repo.py，领域内核=domain/totp.py，存储=data/orm me 域
4 表（user_preferences/device_sessions/totp_credentials/totp_backup_codes，迁移 20261005_f3b9d7e1a5c2，
me:write 种子并入 super_admin/admin/member）；login best-effort 落设备行（fail-open 不阻断登录）。
