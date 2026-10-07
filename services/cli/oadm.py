"""oadm —— ontology-agent 平台运维 CLI（本机/本栈运维域，Click 骨架）。

命令面与设计权威=docs/ops/04-多平台安装部署与CLI执行设计.md §5（批次 A 骨架：doctor/status/
up/down 四件；migrate/config/capability/log 为批次 B 占位，ops/04 §7）。与用户域 `oa`
（services/cli/oa.py）同 Click 形态、同退出码约定；本命令树不调网关 REST，只管本机
文件系统与本 docker compose 栈（§5「只管本机/本栈」）。

退出码（对齐 services/cli/oa.py 模块 docstring 的统一语义）：
    0 成功（doctor/status 全绿、up/down 完成）；
    1 业务/健康失败（doctor·status 存在红项、compose 执行失败——install.sh 第 7 步与 CI
      据此判门禁，ops/04 §6 验收 5）；
    2 用法错误（参数越界；本机前置条件不满足：docker 缺失/compose 文件缺失/命令未实装）。

doctor 八项（ops/04 §3.1 前置自检 + §5）：python 版本 / uv / 磁盘 / 端口占用 / 存储容器
健康 / 迁移版本 / 前端 dist 在场 / 配置档解析，每项绿/红 + 修复指引。端口项对本平台
healthy 容器让行——幂等重装（§6 验收 3）时 doctor 仍可全绿。

测试接缝：外部副作用全部收在模块级小函数（_run/_port_in_use/_docker_ps/_http_probe/
Settings），测试经 monkeypatch 注入桩，零真机依赖（同 tests/cli/test_oa_cli.py 手法）。
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import click
import httpx
from pydantic import ValidationError

from services.cli.oa import EXIT_BUSINESS, EXIT_USAGE  # 退出码常量唯一事实源=oa.py（0/1/2/3/4）
from services.platform.config import Settings

# ---------------------------------------------------------------- 路径与常量

REPO_ROOT = Path(__file__).resolve().parents[2]

# compose 三栈（ops/04 §3：base=存储栈 lite；local=本机 overlay（minio-init 幂等建桶）；
# full=全栈骨架（api+可选 frontend，本批次交付）。容器命名=项目名 onto-agent（compose name）。
_COMPOSE_BASE = ("deploy/docker-compose.yml", "deploy/docker-compose.local.yml")
_COMPOSE_FULL = "deploy/docker-compose.full.yml"
_COMPOSE_PROFILES = ("lite", "full", "full-cad")

_STORAGE_CONTAINERS = ("onto-agent-postgres-1", "onto-agent-redis-1", "onto-agent-minio-1")
_API_CONTAINER = "onto-agent-api-1"

# 端口占用探测（ops/04 §3.1 第 1 步口径：8000/5432/6379/9000）→ 让行容器映射
CHECK_PORTS: tuple[tuple[int, str, str], ...] = (
    (8000, "api", _API_CONTAINER),
    (5432, "postgres", _STORAGE_CONTAINERS[0]),
    (6379, "redis", _STORAGE_CONTAINERS[1]),
    (9000, "minio", _STORAGE_CONTAINERS[2]),
)

_MIN_PY = (3, 11)
_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024  # §3.1：磁盘 ≥2G
_PROBE_TIMEOUT_S = 2.5
_UP_WAIT_TIMEOUT_S = 90.0
_UP_POLL_INTERVAL_S = 3.0
_RUN_TIMEOUT_S = 120.0


# ---------------------------------------------------------------- 副作用接缝（测试注入点）


def _run(cmd: list[str], timeout: float = _RUN_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    """跑外部命令（docker/alembic），捕获输出不抛（rc 与 stderr 由调用方判定）。"""
    try:
        # cmd 由本模块常量拼装（非外部输入），subprocess 直跑无需 shell
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=REPO_ROOT,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=f"{type(exc).__name__}: {exc}")


def _port_in_use(port: int) -> bool:
    """TCP 探测本机端口是否有进程监听（connect_ex 零连接建立，0.3s 超时）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _docker_ps() -> dict[str, str]:
    """onto-agent 项目容器名 → Status 串（docker 不可用返回空 dict=未知态）。"""
    proc = _run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"])
    if proc.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        name, _, status = line.partition("\t")
        if name.startswith("onto-agent-"):
            out[name] = status
    return out


def _http_probe(url: str) -> tuple[bool, str]:
    """GET 探活（LLM/嵌入端点），返回 (是否健康, 说明)。5xx 视为不健康。

    InvalidURL 不在 HTTPError 族内（直承 Exception）——恶意/损坏的端点配置值
    （如 OA_LLM_BASE_URL=http://127.0.0.1:180a2）必须降级为红项说明而非炸 CLI
    （ocr 评审 #2：否则 traceback 污染 status --output json 的 CI 消费契约）。
    """
    try:
        resp = httpx.get(url, timeout=_PROBE_TIMEOUT_S)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return False, f"不可达（{type(exc).__name__}）"
    return resp.status_code < 500, f"HTTP {resp.status_code}"


def _container_healthy(status: str) -> bool:
    """compose healthcheck 绿判据：docker ps Status 含 (healthy)。"""
    return "(healthy)" in status


def _fail(message: str, exit_code: int) -> None:
    """统一错误出口（对齐 oa.py _fail）：stderr 输出 + 指定退出码。"""
    click.echo(f"错误: {message}", err=True)
    ctx = click.get_current_context()
    ctx.exit(exit_code)


# ---------------------------------------------------------------- 检查项（doctor 八项 / status 矩阵）


@dataclass
class CheckResult:
    """单条检查结果：绿/红 + 详情 + 红项修复指引（ops/04 §5「自检八项+修复指引」）。"""

    name: str
    ok: bool
    detail: str
    fix: str = ""


def _check_python() -> CheckResult:
    ver = sys.version_info
    ok = (ver.major, ver.minor) >= _MIN_PY
    return CheckResult(
        "Python ≥3.11",
        ok,
        f"Python {ver.major}.{ver.minor}.{ver.micro}",
        "" if ok else f"安装 Python ≥{_MIN_PY[0]}.{_MIN_PY[1]}（https://www.python.org 或系统包管理器）后重跑",
    )


def _check_uv() -> CheckResult:
    path = shutil.which("uv")
    if path is not None:
        return CheckResult("uv", True, str(path))
    return CheckResult(
        "uv",
        False,
        "未找到 uv 命令",
        "官方自举：Linux/macOS `curl -LsSf https://astral.sh/uv/install.sh | sh`；"
        'Windows `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`（install.sh/ps1 第 2 步同款）',
    )


def _check_disk() -> CheckResult:
    free = shutil.disk_usage(REPO_ROOT).free
    ok = free >= _MIN_FREE_BYTES
    return CheckResult(
        "磁盘 ≥2GiB",
        ok,
        f"可用 {free / (1 << 30):.1f} GiB（{REPO_ROOT}）",
        "" if ok else "清理磁盘空间，或将仓库/数据卷迁移到容量充足的盘后重跑",
    )


def _check_ports() -> CheckResult:
    """端口占用：外部进程=红；本平台 healthy 容器占用=绿（幂等重装让行，见模块 docstring）。"""
    procs = _docker_ps()
    lines: list[str] = []
    bad: list[str] = []
    for port, _piece, container in CHECK_PORTS:
        if not _port_in_use(port):
            lines.append(f"{port} 空闲")
            continue
        if _container_healthy(procs.get(container, "")):
            lines.append(f"{port} 本平台容器占用（{container} healthy，让行）")
            continue
        bad.append(str(port))
        lines.append(f"{port} 被外部进程占用")
    ok = not bad
    return CheckResult(
        "端口占用(8000/5432/6379/9000)",
        ok,
        "；".join(lines),
        ""
        if ok
        else (
            "停用占用进程，或同步修改 deploy/docker-compose*.yml 端口映射与 OA_PG_PORT/"
            "OA_REDIS_URL/OA_MINIO_ENDPOINT 连接配置后重跑"
        ),
    )


def _check_storage() -> CheckResult:
    procs = _docker_ps()
    lines: list[str] = []
    ok = True
    for name in _STORAGE_CONTAINERS:
        status = procs.get(name)
        if status is None:
            lines.append(f"{name} 缺失")
            ok = False
        elif _container_healthy(status):
            lines.append(f"{name} healthy")
        else:
            lines.append(f"{name} 未健康（{status}）")
            ok = False
    api_status = procs.get(_API_CONTAINER)
    if api_status is not None:  # full 档起的 api 容器顺带透出（不在 lite 必查集内）
        lines.append(f"{_API_CONTAINER} {'healthy' if _container_healthy(api_status) else f'未健康（{api_status}）'}")
    return CheckResult(
        "存储容器健康",
        ok,
        "；".join(lines) if procs else f"docker ps 不可用/无容器（{'；'.join(lines)}）",
        ""
        if ok
        else ("oadm up（或 docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml up -d）"),
    )


def _check_migration() -> CheckResult:
    """迁移版本：alembic current 命中 heads 即绿（ops/04 §5 status「迁移版本」同源）。"""
    ini = REPO_ROOT / "services" / "alembic.ini"
    if not ini.is_file():
        return CheckResult(
            "迁移版本",
            False,
            "缺少 services/alembic.ini",
            "确认在仓库根运行（版本包 layouts 见 ops/04 §2）",
        )
    base_cmd = [sys.executable, "-m", "alembic", "-c", str(ini)]
    cur = _run([*base_cmd, "current"])
    heads = _run([*base_cmd, "heads"])
    if cur.returncode != 0 or heads.returncode != 0:
        # alembic 的 FAILED 摘要写 stdout、异常栈写 stderr——取「失败方」流末行做诊断（优先），
        # 失败方零输出时才回退另一方（避免成功方的 head 行掩盖真实失败原因）
        failed = cur if cur.returncode != 0 else heads
        other = heads if failed is cur else cur
        failed_out = ((failed.stderr or "") + (failed.stdout or "")).strip()
        other_out = ((other.stderr or "") + (other.stdout or "")).strip()
        tail = (failed_out or other_out).splitlines()[-1:] or ["无输出"]
        return CheckResult(
            "迁移版本",
            False,
            f"alembic 执行失败：{tail[0][:160]}",
            "多为 PG 连不通或并行会话迁移漂移（共享库竞态口径见 standards/03 §5）：先看存储容器健康项；"
            "PG 在场则核对 .env 的 OA_PG_* 与 alembic -c services/alembic.ini current 诊断",
        )
    cur_rev = next((ln.split()[0] for ln in cur.stdout.splitlines() if ln.strip()), "")
    head_revs = [ln.split()[0] for ln in heads.stdout.splitlines() if ln.strip()]
    if not cur_rev:
        return CheckResult(
            "迁移版本",
            False,
            "尚未迁移（alembic_version 空）",
            "alembic -c services/alembic.ini upgrade heads（oadm migrate 随批次 B 实装）",
        )
    if cur_rev in head_revs:
        return CheckResult("迁移版本", True, f"当前 {cur_rev} = head")
    return CheckResult(
        "迁移版本",
        False,
        f"落后 head（当前 {cur_rev}，目标 {'/'.join(head_revs) or '未知'}）",
        "alembic -c services/alembic.ini upgrade heads",
    )


def _check_frontend_dist() -> CheckResult:
    """前端 dist 在场（ops/04 §2 零 npm：发布包自带预构建产物；缺省=API-only 模式可用但判红提示）。"""
    dist = REPO_ROOT / "frontend" / "dist" / "index.html"
    if dist.is_file():
        return CheckResult("前端 dist", True, "frontend/dist/index.html 在场")
    return CheckResult(
        "前端 dist",
        False,
        "frontend/dist/index.html 缺失",
        "发布包应自带预构建 frontend/dist（流水线产出，ops/04 §2 零 npm）；"
        "开发环境构建前端产物，或明确按 API-only 模式使用（无 Web 界面）",
    )


def _load_settings() -> Settings:
    """配置档解析（唯一取配置入口 services/platform/config.py；解析失败由调用方判红）。"""
    return Settings()


def _check_env_profile() -> CheckResult:
    try:
        settings = _load_settings()
    except ValidationError as exc:
        fields = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors())
        return CheckResult(
            "配置档解析",
            False,
            f".env/OA_* 校验失败：{fields[:200]}",
            "对照 .env.lite/.env.full/.env.full-cad 模板修正对应键值（统一配置层=services/platform/config.py）",
        )
    except Exception as exc:  # 配置层任何加载异常都归「解析红」而非炸 CLI
        return CheckResult(
            "配置档解析",
            False,
            f"加载异常：{type(exc).__name__}: {exc}",
            "检查 .env 语法（KEY=value 逐行）",
        )
    return CheckResult("配置档解析", True, f"档位={settings.deploy_profile}（.env/OA_* 解析通过）")


def _check_llm() -> CheckResult:
    """LLM 端点：未配置=红（透出「当前档缺什么」，ops/04 §4）；已配置探 {base}/models。"""
    try:
        settings = _load_settings()
    except Exception as exc:  # 同配置档解析项：加载异常降级为红项说明，不炸 CLI
        return CheckResult("LLM 端点", False, f"配置档解析失败（{type(exc).__name__}），见 doctor 配置档解析项", "")
    base = settings.llm_base_url
    if not base:
        return CheckResult(
            "LLM 端点",
            False,
            "未配置（OA_LLM_BASE_URL 空）",
            "云渠道：OA_LLM_BASE_URL=https://api.deepseek.com + OA_LLM_API_KEY；"
            "本地 vLLM：http://127.0.0.1:18001/v1 + local-main",
        )
    ok, msg = _http_probe(base.rstrip("/") + "/models")
    return CheckResult(
        "LLM 端点",
        ok,
        f"{base}（model={settings.llm_model}）{msg}",
        "" if ok else f"核对端点可达与密钥：{base}（OA_LLM_BASE_URL/OA_LLM_API_KEY）",
    )


def _check_embed() -> CheckResult:
    """嵌入端点：ollama 协议探 /api/tags，tei 协议探 /health（协议开关=OA_EMBED_PROTOCOL）。"""
    try:
        settings = _load_settings()
    except Exception as exc:  # 同配置档解析项：加载异常降级为红项说明，不炸 CLI
        return CheckResult("嵌入端点", False, f"配置档解析失败（{type(exc).__name__}），见 doctor 配置档解析项", "")
    base = settings.ollama_base_url.rstrip("/")
    if not base:  # 对齐 _check_llm 的空值守卫（ocr 评审 #11：空值时探针退化为相对路径，报错误导）
        return CheckResult(
            "嵌入端点",
            False,
            "未配置（OA_OLLAMA_BASE_URL 空）",
            "部署嵌入服务并配置 OA_OLLAMA_BASE_URL（ollama@11434 或 TEI，见 docs/ops/03）；不配置则向量检索维持降级",
        )
    path = "/health" if settings.embed_protocol == "tei" else "/api/tags"
    ok, msg = _http_probe(base + path)
    return CheckResult(
        "嵌入端点",
        ok,
        f"{settings.embed_protocol} @ {base}{msg}",
        ""
        if ok
        else (
            "部署嵌入服务（ollama@11434 或 TEI，见 docs/ops/03）或暂不启用向量检索"
            f"（memory_vector_enabled=false 时自动降级，当前 {settings.embed_protocol} @ {base} 不可达）"
        ),
    )


def doctor_checks() -> list[CheckResult]:
    """doctor 八项，固定次序（ops/04 §3.1/§5）。"""
    return [
        _check_python(),
        _check_uv(),
        _check_disk(),
        _check_ports(),
        _check_storage(),
        _check_migration(),
        _check_frontend_dist(),
        _check_env_profile(),
    ]


def status_items() -> list[CheckResult]:
    """status 健康矩阵五行（ops/04 §5：存储/迁移版本/LLM/嵌入/前端 dist）。"""
    return [
        _check_storage(),
        _check_migration(),
        _check_llm(),
        _check_embed(),
        _check_frontend_dist(),
    ]


# ---------------------------------------------------------------- compose 组装（up/down 薄壳）


def _compose_files(profile: str) -> list[str]:
    """按档返回 -f 文件集（相对 REPO_ROOT）：lite=存储栈；full/full-cad 追加全栈骨架。"""
    files = [str(REPO_ROOT / f) for f in _COMPOSE_BASE]
    if profile != "lite":
        files.append(str(REPO_ROOT / _COMPOSE_FULL))
    return files


def _compose_base_cmd(files: list[str]) -> list[str]:
    cmd = ["docker", "compose"]
    for f in files:
        cmd += ["-f", f]
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        cmd += ["--env-file", str(env_file)]
    return cmd


def _wait_storage_healthy(
    timeout_s: float = _UP_WAIT_TIMEOUT_S, interval_s: float = _UP_POLL_INTERVAL_S
) -> dict[str, bool]:
    """轮询等待存储容器 healthcheck 转绿（有界；对齐 deploy/up.sh wait_healthy 口径）。"""
    deadline = time.monotonic() + timeout_s
    while True:
        procs = _docker_ps()
        states = {name: _container_healthy(procs.get(name, "")) for name in _STORAGE_CONTAINERS}
        if all(states.values()) or time.monotonic() >= deadline:
            return states
        time.sleep(interval_s)


def _missing_files(files: list[str]) -> list[str]:
    return [f for f in files if not Path(f).is_file()]


# ---------------------------------------------------------------- 命令树（ops/04 §5）


@click.group(name="oadm")
def cli() -> None:
    """oadm —— ontology-agent 平台运维 CLI（只管本机/本栈，docs/ops/04 §5）。"""


@cli.command("doctor")
def doctor() -> None:
    """安装自检八项 + 修复指引（ops/04 §3.1 第 7 步同源；全绿退出 0，任一红退出 1）。"""
    results = doctor_checks()
    for item in results:
        mark = "✓" if item.ok else "✗"
        click.echo(f"{mark} {item.name} — {item.detail}")
        if not item.ok and item.fix:
            click.echo(f"  修复: {item.fix}")
    bad = [item for item in results if not item.ok]
    if bad:
        click.echo(f"\n自检 {len(results) - len(bad)}/{len(results)} 绿，{len(bad)} 红需处理后重跑", err=True)
        ctx = click.get_current_context()
        ctx.exit(EXIT_BUSINESS)
    click.echo(f"\n自检 {len(results)}/{len(results)} 全绿")


@cli.command("status")
@click.option(
    "--output",
    "output",
    type=click.Choice(("text", "json")),
    default="text",
    show_default=True,
    help="输出格式（json 供 CI 消费，ops/04 §6 验收 5）",
)
def status(output: str) -> None:
    """全件健康矩阵：存储/迁移版本/LLM/嵌入/前端 dist（ops/04 §5）。全绿退出 0，任一红退出 1。"""
    items = status_items()
    all_ok = all(item.ok for item in items)
    if output == "json":
        click.echo(json.dumps({"items": [asdict(i) for i in items], "all_ok": all_ok}, ensure_ascii=False, indent=2))
    else:
        for item in items:
            mark = "✓" if item.ok else "✗"
            click.echo(f"{mark} {item.name} — {item.detail}")
            if not item.ok and item.fix:
                click.echo(f"  修复: {item.fix}")
        click.echo(f"\n健康矩阵 {'全绿' if all_ok else '存在红项'}")
    if not all_ok:
        ctx = click.get_current_context()
        ctx.exit(EXIT_BUSINESS)


@cli.command("up")
@click.option(
    "--profile",
    type=click.Choice(_COMPOSE_PROFILES),
    default="lite",
    show_default=True,
    help="部署档（lite=存储栈；full/full-cad 追加 api 服务，差异=外置 CAD 通道）",
)
def up(profile: str) -> None:
    """起栈（docker compose 薄壳，幂等 up -d --remove-orphans），等待并打印各件健康。"""
    files = _compose_files(profile)
    missing = _missing_files(files)
    if missing:
        _fail(f"compose 文件缺失：{', '.join(missing)}（当前档 {profile} 无法起栈）", EXIT_USAGE)
    if shutil.which("docker") is None:
        _fail("未找到 docker 命令：请先安装 Docker Desktop/Engine 后重跑", EXIT_USAGE)
    cmd = _compose_base_cmd(files)
    if profile != "lite":
        cmd += ["--profile", profile]
    cmd += ["up", "-d", "--remove-orphans"]
    click.echo(f"[up] 档位 {profile}：{' '.join(cmd)}")
    proc = _run(cmd, timeout=600.0)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-400:]
        _fail(f"docker compose up 失败（rc={proc.returncode}）：{tail}", EXIT_BUSINESS)
    click.echo("[up] 已提交 compose（幂等）；等待存储健康 ...")
    states = _wait_storage_healthy()
    for name, healthy in states.items():
        click.echo(f"{'✓' if healthy else '✗'} {name}")
    if not all(states.values()):
        click.echo("存储未全绿（首次启动可能仍在初始化）：稍后用 oadm status 复核", err=True)
        ctx = click.get_current_context()
        ctx.exit(EXIT_BUSINESS)
    click.echo("栈已就绪：PG localhost:5432 / Redis localhost:6379 / MinIO localhost:9000(Console 9001)")


@cli.command("down")
@click.option("--volumes", is_flag=True, help="连同删除数据卷（pg_data/redis_data/minio_data）——显式确认后执行，不可逆")
def down(volumes: bool) -> None:
    """停栈（compose down，默认保留数据卷；覆盖全 profile——full 起的 api 一并停）。"""
    files = [f for f in [str(REPO_ROOT / p) for p in (*_COMPOSE_BASE, _COMPOSE_FULL)] if Path(f).is_file()]
    if not files:
        _fail("compose 文件缺失（deploy/docker-compose.yml 不在）", EXIT_USAGE)
    if shutil.which("docker") is None:
        _fail("未找到 docker 命令：请先安装 Docker Desktop/Engine 后重跑", EXIT_USAGE)
    cmd = _compose_base_cmd(files) + ["down"]
    if volumes:
        click.confirm(
            "将删除数据卷 pg_data/redis_data/minio_data（知识/会话/对象数据，不可恢复）！确认继续？",
            abort=True,
        )
        cmd.append("-v")
    click.echo(f"[down] {' '.join(cmd)}")
    proc = _run(cmd, timeout=300.0)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-400:]
        _fail(f"docker compose down 失败（rc={proc.returncode}）：{tail}", EXIT_BUSINESS)
    click.echo("栈已停止" + ("（含数据卷）" if volumes else "（数据卷保留）"))
    if volumes:
        click.echo("提示：~/.oa 本地凭据不在卷内，如需彻底清理请手动删除（ops/04 §6 验收 4 口径）")


# --------------------------------------------------------------- 批次 B 占位（ops/04 §7）


def _not_implemented(name: str) -> None:
    """批次 B 占位：命令树注册在案但未实装，统一按「本机前置不满足」退出 2。"""
    click.echo(f"oadm {name}：未实装（规划批次 B，docs/ops/04 §7）——本期骨架仅 doctor/status/up/down", err=True)
    ctx = click.get_current_context()
    ctx.exit(EXIT_USAGE)


@cli.command("migrate")
def migrate() -> None:
    """alembic 薄壳 upgrade|current|heads（批次 B 占位，当前未实装）。"""
    _not_implemented("migrate")


@cli.command("config")
def config() -> None:
    """配置查看与档位 diff show/diff/set（批次 B 占位，当前未实装）。"""
    _not_implemented("config")


@cli.command("capability")
def capability() -> None:
    """能力面板 CLI 面 list/probe（批次 B 占位，当前未实装）。"""
    _not_implemented("capability")


@cli.command("log")
def log() -> None:
    """栈日志聚合 compose logs 薄壳（批次 B 占位，当前未实装）。"""
    _not_implemented("log")


if __name__ == "__main__":
    cli(prog_name="oadm")
