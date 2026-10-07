"""P-008 多角色测试账号种子——五类角色真实可登录账号（幂等，可重复执行）。

背景（盘点结论，2026-10-07）：平台**无密码重置端点**；建号唯一路径 = invites join
（services/iam/api/users.py 模块头注记「POST /admin/users 不复刻」），join 建号落
随机密码（services/iam/business/invites.py:189）不可知 → 密码固化唯一手段 =
psycopg 直连共享库 UPDATE users.password_hash。哈希格式与
services/platform/security.py:52 hash_password 完全一致
（pbkdf2:sha256:600000$salt_hex$hash_hex，本文件内联同口径实现，不 import 平台包）。

流程（每账号）：
  1. admin 登录 live API 拿 user:write 令牌（POST /api/v1/auth/login）；
  2. users 表查无此邮箱 → POST /api/v1/invites {role, expires_in_hours:24}（201 返回
     明文 token 一次）→ 匿名 POST /api/v1/invites/join {token,email} 建号+绑角色；
     409=邮箱已属他租户 → 显式报错跳过（不硬修）；
  3. 密码确保：先本地 verify（与 security.py:61 verify_password 同算法），不符才
     UPDATE——真幂等（已正确则零写入）；
  4. 角色确保：user_roles 绑定 ≠ 期望 → PATCH /admin/users/{id} {roles:[wanted]}
     （全量替换；可授予白名单=member/curator/ontologist/analyst/admin，见
     services/iam/api/users.py:54）；**guest 不在白名单**、目标持 super_admin 被保护
     （users.py:115 _guard_super_admin）→ DB 直改兜底（删多余绑定+补期望绑定）；
  5. 账号 status ≠ active → PATCH 启用（409 保护时 DB 兜底）；
  6. 验证：每号真登录一次拿 token（POST /auth/login），解 JWT claims 比对角色与 sub。

输出：账号矩阵（邮箱/角色/建号/密码/角色动作/登录验证）+ 汇总；五号全 PASS 退出码 0。

已知坑（首跑实测）：网关写端点 commit 在响应发出之后（services/platform/deps.py:61
generator 依赖 cleanup 序），join/PATCH 200 ≠ 已落库——脚本对「join 后建行可见」
「PATCH 后绑定生效」做轮询等待（wait_until，5s/3s 窗），登录验证非 200 重试一次。

凭据口径：QA 密码 OntoTest@2026 与 admin 缺省口令均为**低敏本地测试凭据**（脚本常量
可接受，禁止用于生产）；DB 连接从 OA_PG_* 环境变量或仓库根 .env 读，缺省
onto/onto_dev@localhost:5432/onto（本地开发库，deploy/docker-compose.yml:8-11 同源默认）。
不改 pyproject：仅用仓内既有依赖 httpx/psycopg。

用法：cd <worktree 根> && uv run python services/devtools/qa/seed-accounts.py
环境变量：OA_QA_BASE_URL（缺省 http://localhost:8364）、OA_QA_ADMIN_EMAIL、
OA_QA_ADMIN_PASSWORD、OA_PG_HOST/PORT/USER/PASSWORD/DB。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psycopg

# ---------------------------------------------------------------- 常量（低敏测试凭据，见模块 docstring）

QA_PASSWORD = "OntoTest@2026"  # 五号统一密码（Onto 前缀 + 年份，符合 Test@2026 规范）

# 五类角色矩阵：邮箱 → 期望唯一角色码（roles 表已种子，见 20260927_f0f79f84dce4 种子迁移）
ACCOUNTS: list[tuple[str, str]] = [
    ("ontologist@test.local", "ontologist"),
    ("curator@test.local", "curator"),
    ("member@test.local", "member"),
    ("guest@test.local", "guest"),
    ("plainadmin@test.local", "admin"),
]

# PATCH /admin/users/{id} 可授予角色白名单（services/iam/api/users.py:54 同口径）；
# 白名单外（guest/super_admin）走 DB 直改兜底
_GRANTABLE_ROLE_CODES = frozenset({"member", "curator", "ontologist", "analyst", "admin"})

# 密码哈希参数（services/platform/security.py:47-49 同值）
_PBKDF2_ALGORITHM = "sha256"
_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16


# ---------------------------------------------------------------- 配置（env > .env > 默认）

def _repo_root() -> Path:
    """services/devtools/qa/seed-accounts.py → 仓库/worktree 根（上三级）。"""
    return Path(__file__).resolve().parents[3]


def _dotenv() -> dict[str, str]:
    """解析仓库根 .env（KEY=VALUE，# 注释行跳过）；不存在返回空。真实环境变量优先。"""
    path = _repo_root() / ".env"
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


_DOTENV = _dotenv()


def _env(key: str, default: str) -> str:
    return os.environ.get(key) or _DOTENV.get(key) or default


@dataclass
class Config:
    base_url: str
    admin_email: str
    admin_password: str
    pg: dict[str, str]  # psycopg connect kwargs


def load_config() -> Config:
    return Config(
        base_url=_env("OA_QA_BASE_URL", "http://localhost:8364").rstrip("/"),
        admin_email=_env("OA_QA_ADMIN_EMAIL", "admin@ontology.local"),
        admin_password=_env("OA_QA_ADMIN_PASSWORD", "OntoAdmin@2026"),
        pg={
            "host": _env("OA_PG_HOST", "localhost"),
            "port": _env("OA_PG_PORT", "5432"),
            "dbname": _env("OA_PG_DB", "onto"),
            "user": _env("OA_PG_USER", "onto"),
            "password": _env("OA_PG_PASSWORD", "onto_dev"),
            "connect_timeout": "5",
        },
    )


# ---------------------------------------------------------------- 密码哈希（security.py:52/61 内联同口径）

def hash_password(password: str) -> str:
    """与 services/platform/security.py:52 hash_password 逐字同格式（勿改参数，改则登录失效）。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(_PBKDF2_ALGORITHM, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2:{_PBKDF2_ALGORITHM}:{_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str | None) -> bool:
    """与 services/platform/security.py:61 verify_password 同算法（解析失败一律 False）。"""
    if not password_hash:
        return False
    try:
        scheme, algorithm, iterations, salt_hex, digest_hex = password_hash.replace(":", "$").split("$")
        if scheme != "pbkdf2" or algorithm != _PBKDF2_ALGORITHM:
            return False
        actual = hashlib.pbkdf2_hmac(
            _PBKDF2_ALGORITHM, password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(actual, bytes.fromhex(digest_hex))


def jwt_claims_unverified(token: str) -> dict:
    """解 JWT payload（不验签，仅矩阵展示用；真实校验由服务端登录 200 背书）。"""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


# ---------------------------------------------------------------- API 客户端（token 不落日志）

class Api:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = httpx.Client(base_url=cfg.base_url, timeout=15.0)
        self.admin_token: str | None = None

    def _auth_headers(self) -> dict[str, str]:
        assert self.admin_token, "admin 未登录"
        return {"Authorization": f"Bearer {self.admin_token}"}

    def login_admin(self) -> dict:
        """admin 登录并预检 user:write scope（invites 管理面门禁，invites.py:38）。"""
        resp = self.client.post(
            "/api/v1/auth/login",
            json={"email": self.cfg.admin_email, "password": self.cfg.admin_password},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"admin 登录失败 HTTP {resp.status_code}: {resp.text[:200]}")
        claims = jwt_claims_unverified(resp.json()["access_token"])
        if "user:write" not in claims.get("scopes", []):
            raise RuntimeError(f"admin 令牌缺 user:write scope（roles={claims.get('roles')}），无法生成邀请")
        self.admin_token = resp.json()["access_token"]
        return claims

    def create_invite(self, role: str) -> str:
        """POST /invites → 明文 token（仅本次返回；201）。"""
        resp = self.client.post(
            "/api/v1/invites", json={"role": role, "expires_in_hours": 24}, headers=self._auth_headers()
        )
        if resp.status_code != 201:
            raise RuntimeError(f"POST /invites HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()["token"]

    def join(self, token: str, email: str, display_name: str) -> httpx.Response:
        """匿名 POST /invites/join（建号/幂等补授；409=他租户冲突；410=邀请失效）。"""
        return self.client.post(
            "/api/v1/invites/join", json={"token": token, "email": email, "display_name": display_name}
        )

    def patch_user(self, user_id: str, body: dict) -> httpx.Response:
        return self.client.patch(f"/api/v1/admin/users/{user_id}", json=body, headers=self._auth_headers())

    def login(self, email: str, password: str) -> httpx.Response:
        return self.client.post("/api/v1/auth/login", json={"email": email, "password": password})


# ---------------------------------------------------------------- DB 侧（共享库直改；只动测试账号行）

def find_user_rows(cur: psycopg.Cursor, email: str) -> list[dict]:
    """同邮箱全部行（多租户可能多行），created_at 升序——与登录取行口径对齐：
    登录=最早 active 行（services/iam/api/auth.py:120-124），无 active 行时取最早行以便修复启停。"""
    cur.execute(
        "SELECT id::text, tenant_id::text, status, password_hash FROM users "
        "WHERE email = %s ORDER BY created_at ASC",
        (email,),
    )
    rows = [
        {"id": r[0], "tenant_id": r[1], "status": r[2], "password_hash": r[3]} for r in cur.fetchall()
    ]
    active = [r for r in rows if r["status"] == "active"]
    return active if active else rows


def roles_of(cur: psycopg.Cursor, user_id: str) -> set[str]:
    cur.execute(
        "SELECT r.code FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = %s::uuid",
        (user_id,),
    )
    return {r[0] for r in cur.fetchall()}


def set_password(cur: psycopg.Cursor, user_id: str) -> None:
    cur.execute(
        "UPDATE users SET password_hash = %s, updated_at = now() WHERE id = %s::uuid",
        (hash_password(QA_PASSWORD), user_id),
    )


def fix_roles_direct(cur: psycopg.Cursor, user_id: str, tenant_id: str, wanted: str) -> None:
    """DB 直改兜底（guest 不在 PATCH 白名单 / super_admin 保护时）：删多余绑定 + 补期望绑定。"""
    cur.execute("SELECT id::text FROM roles WHERE code = %s", (wanted,))
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"roles 表无角色码: {wanted}")
    role_id = row[0]
    cur.execute("DELETE FROM user_roles WHERE user_id = %s::uuid AND role_id <> %s::uuid", (user_id, role_id))
    cur.execute(
        "INSERT INTO user_roles (id, tenant_id, user_id, role_id) "
        "VALUES (%s, %s::uuid, %s::uuid, %s::uuid) ON CONFLICT (user_id, role_id) DO NOTHING",
        (str(uuid.uuid4()), tenant_id, user_id, role_id),  # id 列无 server_default，应用侧生成
    )


# ---------------------------------------------------------------- 单账号处理

@dataclass
class Result:
    email: str
    role: str
    created: str = "existing"  # created / existing / error:…
    password: str = "-"  # ok（已正确，零写入）/ set / error:…
    role_action: str = "-"  # ok / patched / db-fixed / error:…
    login: str = "FAIL"  # PASS / FAIL / SKIP
    login_roles: list[str] = field(default_factory=list)
    note: str = ""


def wait_until(fn, timeout_s: float, interval_s: float = 0.1):
    """轮询到 fn() 返回真值或超时（返回 None）。

    必要性：网关写端点的 commit 在**响应发出之后**（services/platform/deps.py:61
    generator 依赖 cleanup 先后序），join/PATCH 的 200 不代表已落库——立即回读有竞态
    （首跑实测五号全踩：join 200 但紧随 SELECT 查无行，行在毫秒级后落库）。
    """
    deadline = time.monotonic() + timeout_s
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval_s)


def process_account(api: Api, cur: psycopg.Cursor, email: str, role: str) -> Result:
    result = Result(email=email, role=role)
    try:
        # ① 建号（仅在库无此邮箱时走 API；已存在则跳过——join 幂等但会白耗一个 invite）
        rows = find_user_rows(cur, email)
        if not rows:
            token = api.create_invite(role)
            resp = api.join(token, email, display_name=f"QA {role}")
            if resp.status_code == 409:
                result.created = "error:他租户邮箱冲突"
                result.note = resp.text[:120]
                return result
            if resp.status_code != 200:
                result.created = f"error:join HTTP {resp.status_code}"
                result.note = resp.text[:120]
                return result
            rows = wait_until(lambda: find_user_rows(cur, email), timeout_s=5.0)
            if not rows:
                result.created = "error:join 200 后 5s 内未见用户行"
                result.note = "疑 commit 延迟超轮询窗（deps.py:61）"
                return result
            result.created = "created"
        user = rows[0]
        user_id, tenant_id = user["id"], user["tenant_id"]

        # ② 密码确保（先 verify，正确则零写入——真幂等）
        if verify_password(QA_PASSWORD, user["password_hash"]):
            result.password = "ok"
        else:
            set_password(cur, user_id)
            cur.connection.commit()
            result.password = "set"

        # ③ 启停确保（disabled 账号登录 401）
        if user["status"] != "active":
            resp = api.patch_user(user_id, {"status": "active"})
            if resp.status_code == 200:
                wait_until(lambda: find_user_rows(cur, email)[0]["status"] == "active", timeout_s=3.0)
            else:  # super_admin 保护等 → DB 兜底
                cur.execute("UPDATE users SET status = 'active', updated_at = now() WHERE id = %s::uuid", (user_id,))
                cur.connection.commit()
            result.note = (result.note + " " if result.note else "") + "re-activated"

        # ④ 角色确保（绑定集合 ≠ 期望才动）
        have = roles_of(cur, user_id)
        if have == {role}:
            result.role_action = "ok"
        elif role in _GRANTABLE_ROLE_CODES and "super_admin" not in have:
            resp = api.patch_user(user_id, {"roles": [role]})  # 全量替换（users.py:160-181）
            settled = (
                resp.status_code == 200
                and wait_until(lambda: roles_of(cur, user_id) == {role}, timeout_s=3.0) is not None
            )
            if settled:
                result.role_action = "patched"
            else:  # 白名单/保护/提交窗超时之外的非预期失败 → DB 兜底
                fix_roles_direct(cur, user_id, tenant_id, role)
                cur.connection.commit()
                result.role_action = "db-fixed"
                result.note = (result.note + " " if result.note else "") + f"PATCH回退(HTTP {resp.status_code})"
        else:
            fix_roles_direct(cur, user_id, tenant_id, role)
            cur.connection.commit()
            result.role_action = "db-fixed"
            if role not in _GRANTABLE_ROLE_CODES:
                result.note = (result.note + " " if result.note else "") + "guest 不在 PATCH 白名单，走 DB"

        # ⑤ 真登录验证（每号一次；解 claims 比对角色与 sub；非 200 重试一次防提交窗）
        resp = api.login(email, QA_PASSWORD)
        if resp.status_code != 200:
            time.sleep(0.5)
            resp = api.login(email, QA_PASSWORD)
        if resp.status_code != 200:
            result.login = "FAIL"
            result.note = (result.note + " " if result.note else "") + f"登录 HTTP {resp.status_code}: {resp.text[:80]}"
            return result
        pair = resp.json()
        claims = jwt_claims_unverified(pair["access_token"])
        result.login_roles = sorted(claims.get("roles", []))
        if role in claims.get("roles", []) and claims.get("sub") == user_id:
            result.login = "PASS"
        else:
            result.note = (result.note + " " if result.note else "") + "claims 角色/sub 与期望不符"
    except Exception as exc:  # noqa: BLE001 — 单账号失败不阻断其余账号
        result.note = (result.note + " " if result.note else "") + f"{type(exc).__name__}: {exc}"[:200]
        result.login = "FAIL"
    return result


# ---------------------------------------------------------------- 主流程

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows 控制台中文安全
    cfg = load_config()
    print(f"[seed-accounts] live={cfg.base_url}  pg={cfg.pg['user']}@{cfg.pg['host']}:{cfg.pg['port']}/{cfg.pg['dbname']}")
    print(f"[seed-accounts] QA 密码（五号统一）: {QA_PASSWORD}")

    api = Api(cfg)
    admin_claims = api.login_admin()
    print(
        f"[seed-accounts] admin={cfg.admin_email} roles={admin_claims.get('roles')} "
        f"user:write=OK（预检通过）"
    )

    with psycopg.connect(**cfg.pg) as conn:  # type: ignore[arg-type]
        with conn.cursor() as cur:
            results = [process_account(api, cur, email, role) for email, role in ACCOUNTS]

    # 账号矩阵
    print("\n=== 账号矩阵 ===")
    header = f"{'邮箱':<24} {'角色':<12} {'建号':<10} {'密码':<6} {'角色动作':<10} {'登录':<6} 实际roles"
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.email:<24} {r.role:<12} {r.created:<10} {r.password:<6} "
            f"{r.role_action:<10} {r.login:<6} {','.join(r.login_roles)}"
        )
        if r.note:
            print(f"{'':<24} └ note: {r.note}")

    passed = sum(1 for r in results if r.login == "PASS")
    print(f"\n=== 汇总: {passed}/{len(results)} PASS ===")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
