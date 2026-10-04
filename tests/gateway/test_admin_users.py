"""用户管理 CRUD 四端点集成测试（api/01 §5.8 users CRUD；standards/01 §2.9 AAA/中文命名）。

覆盖（B8-WA 切片，契约源=前端追认口径——形状权威=features/admin/api.ts AdminUser +
mocks/admin-handlers.ts users 段）：
- 列表：信封 {items, next_cursor:null}、AdminUser 字段形状（含 roles 数组）、query/status/role 三维筛选；
- 创建路径缺席确认：POST /admin/users 不存在（建号唯一路径=invites join，B8-WA 跳过不复刻）；
- PATCH：display_name 改名、status 启停可逆、roles 数组全量替换（白名单外 409 / 未知码 422）、密码不触碰；
- DELETE：软删 status=disabled（不物理删）、已禁用重复删除 200 幂等、禁删自己 409、super_admin 保护 409、审计留痕；
- scope 门禁：无 user:read/user:write → 403+2001；
- 详情：404（不存在/跨租户同口径）。

装配样板复用 tests/gateway/test_invites.py invite_env（lite 档 Settings + fakeredis + 环境 PG，
PG 不可达即 skip）；Windows Selector 循环固定策略见 test_invites.py 文件头。

已知形状差异（对齐 W-C，后端做不到的 mock 形状清单）：
1. department：users 表无列（迁移冻结）→ 恒回 "—" 占位，PATCH 接受但不落库；
2. status='invited'：mock 有、真实数据无（pending 邀请人无 users 行，CHECK 约束限 active/disabled）；
3. invited_via/invite_link_id：mock invited 行携带 → 后端恒 null；
4. mock DELETE 204 空体 → 后端 200 信封 {id, status:"disabled"}（幂等可观测，invites 撤销同口径）；
5. mock 404 错误码 4041 → 后端统一 404（services 全局既有口径）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid

import pytest

if sys.platform == "win32":
    # psycopg async 仅支持 selector 事件循环（Windows 默认 Proactor 不兼容，pytest-asyncio 逐用例建 loop）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from fakeredis import aioredis as fakeredis_aio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from services.gateway.app import create_app
from services.iam.data.orm import AuditLog, Role, Tenant, User, UserRole
from services.platform import deps
from services.platform.config import Settings
from services.platform.security import hash_password

_SECRET = "unit-test-secret-0123456789abcdef0123456789"  # ≥32 字节（RFC 7518 HS256 密钥长度下限）

_USERS = "/api/v1/admin/users"  # api/01 §5.8 users CRUD 四端点前缀
_PASSWORD = "ItPassword!1"


def _fake_redis() -> fakeredis_aio.FakeRedis:
    return fakeredis_aio.FakeRedis(decode_responses=True)


async def _login_headers(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
async def users_env(monkeypatch):
    """integration 装配（test_invites.py invite_env 同款）：lite 档 + fakeredis + 环境 PG。

    另设：admin 用户（绑种子 admin 角色→含 user:read/user:write）+ 无角色用户（scope 门禁反例）。
    """
    settings = Settings(jwt_secret=_SECRET, deploy_profile="lite")
    fake_redis = _fake_redis()
    monkeypatch.setattr(deps, "get_redis", lambda _settings: fake_redis)

    # 环境守卫：PG 不可达或 users 表未迁移 → 跳过（同 test_auth/test_invites 前置口径）
    try:
        async with deps.get_engine(settings).connect() as conn:
            await conn.execute(text("SELECT 1 FROM users LIMIT 1"))
    except Exception:  # noqa: BLE001  ——环境不就绪即跳过
        deps.get_engine.cache_clear()
        pytest.skip("环境 PG 不可达或 users 表未迁移，跳过 integration 用例")

    suffix = uuid.uuid4().hex[:8]
    admin_email = f"users-it-admin-{suffix}@test.local"
    password = _PASSWORD
    plain_email = f"users-it-plain-{suffix}@test.local"
    tenant_id, admin_id, plain_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    factory = deps.get_session_factory(settings)
    async with factory() as session:
        admin_role = (await session.execute(select(Role).where(Role.code == "admin"))).scalar_one()
        session.add(
            Tenant(
                id=tenant_id,
                name=f"users-it-{suffix}",
                slug=f"users-it-{suffix}",
                plan="free",
                settings={"governance_tier": "solo"},
                status="active",
            )
        )
        session.add(
            User(
                id=admin_id,
                tenant_id=tenant_id,
                email=admin_email,
                password_hash=hash_password(password),
                display_name="users-it-admin",
                status="active",
            )
        )
        session.add(UserRole(tenant_id=tenant_id, user_id=admin_id, role_id=admin_role.id))  # admin→含 user:read/write
        session.add(
            User(  # 无任何角色绑定 → scopes 空 → scope 门禁反例（2001）
                id=plain_id,
                tenant_id=tenant_id,
                email=plain_email,
                password_hash=hash_password(password),
                display_name="users-it-plain",
                status="active",
            )
        )
        await session.commit()

    app = create_app(settings)
    app.state.audit_session_factory = factory  # 审计中间件落库面（lifespan 不触发，手动装配）
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")
    env = {
        "settings": settings,
        "factory": factory,
        "tenant_id": tenant_id,
        "admin_id": admin_id,
        "admin": {"email": admin_email, "password": password},
        "plain": {"email": plain_email, "password": password},
        "extra_tenant_ids": [],  # 用例内追加租户（统一清理）
        "extra_user_ids": [],
    }
    try:
        yield client, env
    finally:
        await client.aclose()
        async with factory() as session:  # 每用例自清理（standards/01 §2.9；FK 逆序）
            tenant_ids = [tenant_id, *env["extra_tenant_ids"]]
            user_ids = [admin_id, plain_id, *env["extra_user_ids"]]
            await session.execute(text("DELETE FROM user_roles WHERE tenant_id = ANY(:ids)").bindparams(ids=tenant_ids))
            await session.execute(text("DELETE FROM users WHERE id = ANY(:ids)").bindparams(ids=user_ids))
            await session.execute(text("DELETE FROM tenants WHERE id = ANY(:ids)").bindparams(ids=tenant_ids))
            await session.commit()
        await deps.dispose_gateways(settings)
        deps.get_engine.cache_clear()
        if hasattr(deps.get_redis, "cache_clear"):  # monkeypatch 替身无 lru_cache 属性
            deps.get_redis.cache_clear()


async def _seed_user(
    env: dict,
    *,
    email: str,
    display_name: str,
    status: str = "active",
    roles: tuple[str, ...] = (),
) -> uuid.UUID:
    """直插一个本租户用户并绑角色（返回 user_id，自动登记清理）。"""
    user_id = uuid.uuid4()
    async with env["factory"]() as session:
        session.add(
            User(
                id=user_id,
                tenant_id=env["tenant_id"],
                email=email,
                username=email.split("@")[0],
                password_hash=hash_password(_PASSWORD),
                display_name=display_name,
                status=status,
            )
        )
        for code in roles:
            role = (await session.execute(select(Role).where(Role.code == code))).scalar_one()
            session.add(UserRole(tenant_id=env["tenant_id"], user_id=user_id, role_id=role.id))
        await session.commit()
    env["extra_user_ids"].append(user_id)
    return user_id


# ================================================================ ① 列表：信封与字段形状（含 roles）


@pytest.mark.integration
async def test_列表_信封形状与字段逐项_含roles数组(users_env):
    client, env = users_env
    headers = await _login_headers(client, **env["admin"])
    uid = await _seed_user(env, email="wang.it@example.com", display_name="王工", roles=("curator", "ontologist"))
    # Act
    resp = await client.get(_USERS, headers=headers)
    # Assert：信封 {items, next_cursor:null}；AdminUser 字段形状逐项对齐 mock（契约源=前端追认）
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["next_cursor"] is None
    item = next(u for u in body["items"] if u["id"] == str(uid))
    assert item["email"] == "wang.it@example.com"
    assert item["username"] == "wang.it"  # DB username 回显（建号路径写入 local-part）
    assert item["display_name"] == "王工"
    assert item["roles"] == ["curator", "ontologist"]  # 角色码数组（绑定创建序）
    assert item["department"] == "—"  # 已知形状差异：users 表无列，mock 同款占位
    assert item["status"] == "active"
    assert item["last_login_at"] is None
    assert set(item) >= {"id", "username", "email", "display_name", "roles", "department", "status", "last_login_at"}


# ================================================================ ② 列表：query / status / role 三维筛选


@pytest.mark.integration
async def test_列表筛选_status_role_query_组合生效(users_env):
    client, env = users_env
    headers = await _login_headers(client, **env["admin"])
    curator_id = await _seed_user(env, email="curator.f@example.com", display_name="李倩", roles=("curator",))
    disabled_id = await _seed_user(
        env, email="member.d@example.com", display_name="钱进", status="disabled", roles=("member",)
    )
    ontologist_id = await _seed_user(env, email="ontologist.o@example.com", display_name="赵敏", roles=("ontologist",))

    async def listed(resp) -> set[str]:
        assert resp.status_code == status.HTTP_200_OK, resp.text
        return {u["id"] for u in resp.json()["items"]}  # JSON 侧 id 均为字符串

    # Act + Assert：status=disabled 只剩停用行（invited 恒空集——pending 邀请人无 users 行）
    ids = await listed(await client.get(_USERS, params={"status": "disabled"}, headers=headers))
    assert str(disabled_id) in ids and str(curator_id) not in ids
    ids = await listed(await client.get(_USERS, params={"status": "invited"}, headers=headers))
    assert ids == set()
    # role=curator 只剩绑定行（join user_roles→roles 精确码匹配）
    ids = await listed(await client.get(_USERS, params={"role": "curator"}, headers=headers))
    assert ids == {str(curator_id)}
    # query 关键词命中 email / display_name / username 子串
    ids = await listed(await client.get(_USERS, params={"query": "curator.f"}, headers=headers))
    assert ids == {str(curator_id)}
    ids = await listed(await client.get(_USERS, params={"query": "赵敏"}, headers=headers))
    assert ids == {str(ontologist_id)}  # display_name 子串命中
    ids = await listed(await client.get(_USERS, params={"query": "无此人xyz"}, headers=headers))
    assert ids == set()
    # 组合：status=active + role=curator
    ids = await listed(await client.get(_USERS, params={"status": "active", "role": "curator"}, headers=headers))
    assert ids == {str(curator_id)}


# ================================================================ ③ 创建路径缺席确认（建号唯一路径=invites join）


@pytest.mark.integration
async def test_创建路径缺席_POST不存在_建号唯一路径为invites_join(users_env):
    client, env = users_env
    headers = await _login_headers(client, **env["admin"])
    # Act：POST /admin/users（mock 的邮箱批量邀请端点，后端不复刻）
    resp = await client.post(_USERS, json={"emails": ["x@example.com"], "role": "member"}, headers=headers)
    # Assert：405（路由缺席）+ 统一错误体（api/01 §4 四字段契约；建号唯一路径=invites join）
    assert resp.status_code == status.HTTP_405_METHOD_NOT_ALLOWED, resp.text
    assert "code" in resp.json()


# ================================================================ ④ PATCH：改名 + 角色全量替换 + 密码不触碰


@pytest.mark.integration
async def test_PATCH_改名与角色全量替换_密码哈希不触碰(users_env):
    client, env = users_env
    headers = await _login_headers(client, **env["admin"])
    uid = await _seed_user(env, email="patch.me@example.com", display_name="旧名", roles=("member",))
    # Act：改名 + roles 全量替换（member → curator+member 两码，请求序即返回序）
    resp = await client.patch(
        f"{_USERS}/{uid}", json={"display_name": "新名", "roles": ["curator", "member"]}, headers=headers
    )
    # Assert：200 = 更新后完整用户对象（roles 请求序）
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["id"] == str(uid) and body["display_name"] == "新名" and body["roles"] == ["curator", "member"]
    assert body["status"] == "active" and "email" in body  # 完整对象而非增量
    # Assert：DB 绑定全量替换（旧 member 绑定不复存为重复行）且 password_hash 未触碰
    async with env["factory"]() as session:
        row = await session.get(User, uid)
        assert row is not None and row.display_name == "新名"
        assert row.password_hash.startswith("pbkdf2:")  # CRUD 不触碰密码（只读比对格式）
        codes = (
            await session.execute(
                select(Role.code).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == uid)
            )
        ).scalars().all()
        assert sorted(codes) == ["curator", "member"]


# ================================================================ ⑤ PATCH：status 启停可逆（DELETE 软删的回程）


@pytest.mark.integration
async def test_PATCH_停用与启用_可逆出口(users_env):
    client, env = users_env
    headers = await _login_headers(client, **env["admin"])
    uid = await _seed_user(env, email="toggle@example.com", display_name="启停员")
    # Act ①：停用
    off = await client.patch(f"{_USERS}/{uid}", json={"status": "disabled"}, headers=headers)
    # Act ②：启用回程（前端「启用」按钮口径）
    on = await client.patch(f"{_USERS}/{uid}", json={"status": "active"}, headers=headers)
    # Assert：两向 200 且 status 落库
    assert off.status_code == status.HTTP_200_OK and off.json()["status"] == "disabled", off.text
    assert on.status_code == status.HTTP_200_OK and on.json()["status"] == "active", on.text
    async with env["factory"]() as session:
        row = await session.get(User, uid)
        assert row is not None and row.status == "active"


# ================================================================ ⑥ DELETE：软删 + 幂等 + 不物理删 + 审计留痕


@pytest.mark.integration
async def test_DELETE_软删幂等_不物理删_密码保留_审计留痕(users_env):
    client, env = users_env
    headers = await _login_headers(client, **env["admin"])
    uid = await _seed_user(env, email="soft.del@example.com", display_name="软删员", roles=("member",))
    # Act ①：首次删除 → 200 信封 {id, status:"disabled"}
    first = await client.delete(f"{_USERS}/{uid}", headers=headers)
    # Act ②：重复删除（已禁用）→ 200 幂等同响应
    repeat = await client.delete(f"{_USERS}/{uid}", headers=headers)
    # Assert ①②：非空体信封（禁 204 口径），两向同响应
    assert first.status_code == status.HTTP_200_OK, first.text
    assert first.json() == {"id": str(uid), "status": "disabled"}
    assert repeat.status_code == status.HTTP_200_OK and repeat.json() == first.json()
    # Assert：行仍在（不物理删，users 被 FK 引用）、status=disabled、密码保留、角色绑定保留
    async with env["factory"]() as session:
        row = await session.get(User, uid)
        assert row is not None and row.status == "disabled" and row.password_hash.startswith("pbkdf2:")
        bindings = (await session.execute(select(UserRole).where(UserRole.user_id == uid))).scalars().all()
        assert len(bindings) == 1
        # 审计留痕（08 §3 网关中间件）：DELETE 动作落 audit_logs（路径模板归一 {id}）
        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.action == "DELETE /api/v1/admin/users/{id}", AuditLog.tenant_id == env["tenant_id"])
                .order_by(AuditLog.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        assert audit is not None and audit.result == "success" and audit.actor_id == env["admin_id"]


# ================================================================ ⑦ DELETE：禁删自己 409


@pytest.mark.integration
async def test_DELETE_禁删自己_409(users_env):
    client, env = users_env
    headers = await _login_headers(client, **env["admin"])
    # Act：管理员删除自己（principal 对比）
    resp = await client.delete(f"{_USERS}/{env['admin_id']}", headers=headers)
    # Assert：409 + 3409；自己仍 active
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert resp.json()["code"] == 3409
    async with env["factory"]() as session:
        row = await session.get(User, env["admin_id"])
        assert row is not None and row.status == "active"


# ================================================================ ⑧ 越权角色授予 409（super_admin 底线 + 白名单外）


@pytest.mark.integration
async def test_越权角色授予_409_superadmin不可授予_白名单外同拒(users_env):
    client, env = users_env
    headers = await _login_headers(client, **env["admin"])
    uid = await _seed_user(env, email="grant.me@example.com", display_name="受权员", roles=("member",))
    # Act ①：授予 super_admin（平台保留角色）→ 409
    grant_super = await client.patch(f"{_USERS}/{uid}", json={"roles": ["super_admin"]}, headers=headers)
    # Act ②：白名单外未开放码（guest）→ 409 同拒
    grant_guest = await client.patch(f"{_USERS}/{uid}", json={"roles": ["guest"]}, headers=headers)
    # Assert ①②：409 + 3409（越权），绑定未被改动
    assert grant_super.status_code == status.HTTP_409_CONFLICT, grant_super.text
    assert grant_super.json()["code"] == 3409
    assert grant_guest.status_code == status.HTTP_409_CONFLICT, grant_guest.text
    async with env["factory"]() as session:
        codes = (
            await session.execute(
                select(Role.code).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == uid)
            )
        ).scalars().all()
        assert codes == ["member"]
    # Act ③：super_admin 持有者受保护——不可停用/删改（先直插绑定再打）
    super_id = await _seed_user(env, email="super.holder@example.com", display_name="超管", roles=("super_admin",))
    disable_super = await client.patch(f"{_USERS}/{super_id}", json={"status": "disabled"}, headers=headers)
    delete_super = await client.delete(f"{_USERS}/{super_id}", headers=headers)
    rebind_super = await client.patch(f"{_USERS}/{super_id}", json={"roles": ["member"]}, headers=headers)
    # Assert ③：三向全 409（不可删/停/改绑）
    assert disable_super.status_code == status.HTTP_409_CONFLICT, disable_super.text
    assert delete_super.status_code == status.HTTP_409_CONFLICT, delete_super.text
    assert rebind_super.status_code == status.HTTP_409_CONFLICT, rebind_super.text


# ================================================================ ⑨ PATCH：白名单内未知码 422（roles 表未种子化）


@pytest.mark.integration
async def test_PATCH_白名单内未种子化角色码_422(users_env):
    client, env = users_env
    headers = await _login_headers(client, **env["admin"])
    uid = await _seed_user(env, email="analyst.want@example.com", display_name="分析员")
    # Act：analyst 在白名单但 roles 表未种子化（配置漂移显式暴露，invites 未知角色码同口径）
    resp = await client.patch(f"{_USERS}/{uid}", json={"roles": ["analyst"]}, headers=headers)
    # Assert：422 + 3001（PARAM_INVALID）
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    assert resp.json()["code"] == 3001


# ================================================================ ⑩ 无 scope：403+2001（read 与 write 双面）


@pytest.mark.integration
async def test_无scope用户_403_2001_read与write双面(users_env):
    client, env = users_env
    plain_headers = await _login_headers(client, **env["plain"])  # 无角色绑定 → scopes 空
    uid = await _seed_user(env, email="target@example.com", display_name="目标员")
    # Act：读面（列表/详情）与写面（PATCH/DELETE）各一发
    list_resp = await client.get(_USERS, headers=plain_headers)
    detail_resp = await client.get(f"{_USERS}/{uid}", headers=plain_headers)
    patch_resp = await client.patch(f"{_USERS}/{uid}", json={"display_name": "改名"}, headers=plain_headers)
    delete_resp = await client.delete(f"{_USERS}/{uid}", headers=plain_headers)
    # Assert：deny-by-default（08 §2.5 PDP 第 3 步）——读 403+required user:read，写 403+required user:write
    assert list_resp.status_code == status.HTTP_403_FORBIDDEN and list_resp.json()["detail"]["required"] == "user:read"
    assert detail_resp.status_code == status.HTTP_403_FORBIDDEN
    patch_body = patch_resp.json()
    assert patch_resp.status_code == status.HTTP_403_FORBIDDEN and patch_body["detail"]["required"] == "user:write"
    assert delete_resp.status_code == status.HTTP_403_FORBIDDEN


# ================================================================ ⑪ 详情：含 roles 数组；404（不存在/跨租户同口径）


@pytest.mark.integration
async def test_详情_含roles_不存在与跨租户_404(users_env):
    client, env = users_env
    headers = await _login_headers(client, **env["admin"])
    uid = await _seed_user(env, email="detail@example.com", display_name="详请员", roles=("ontologist",))
    # Act ①：详情
    ok_resp = await client.get(f"{_USERS}/{uid}", headers=headers)
    # Act ②：不存在的 id
    missing_resp = await client.get(f"{_USERS}/{uuid.uuid4()}", headers=headers)
    # Act ③：跨租户同 id 形状（他租户用户）→ 同口径 404 防租户枚举
    other_tenant_id, other_user_id = uuid.uuid4(), uuid.uuid4()
    async with env["factory"]() as session:
        session.add(
            Tenant(
                id=other_tenant_id,
                name="users-it-other",
                slug=f"users-it-other-{uuid.uuid4().hex[:8]}",
                plan="free",
                settings={"governance_tier": "solo"},
                status="active",
            )
        )
        session.add(
            User(
                id=other_user_id,
                tenant_id=other_tenant_id,
                email="elsewhere@example.com",
                password_hash="it-only",
                status="active",
            )
        )
        await session.commit()
    env["extra_tenant_ids"].append(other_tenant_id)
    env["extra_user_ids"].append(other_user_id)
    cross_resp = await client.get(f"{_USERS}/{other_user_id}", headers=headers)
    # Assert：详情含 roles 数组；两向 404 统一错误体
    assert ok_resp.status_code == status.HTTP_200_OK, ok_resp.text
    assert ok_resp.json()["roles"] == ["ontologist"]
    assert missing_resp.status_code == status.HTTP_404_NOT_FOUND, missing_resp.text
    assert missing_resp.json()["code"] == 404
    assert cross_resp.status_code == status.HTTP_404_NOT_FOUND, cross_resp.text
    assert cross_resp.json()["code"] == 404
