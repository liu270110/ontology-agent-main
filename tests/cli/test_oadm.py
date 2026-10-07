"""oadm 运维 CLI 骨架测试（CliRunner + 模块级副作用桩注入，零外网零真机 docker）。

覆盖（standards/01 §2.9：AAA 布局、中文命名；手法同 tests/cli/test_oa_cli.py）：
- 命令树装配：ops/04 §5 命令面在册（doctor/status/up/down + 批次 B 四占位）；
- doctor：八项判定逐项桩测（python/uv/磁盘/端口/存储/迁移/dist/配置档），
  全绿退出 0、任一红退出 1 且渲染修复指引；
- status：--output json 形状（items/all_ok，ops/04 §6 验收 5 的 CI 消费面）与文本渲染；
- up：compose -f 组合按档选择（lite=base+local；full/full-cad 追加 full 骨架+--profile）、
  幂等 up -d --remove-orphans、compose 文件缺失/docker 缺失=用法错误 2、执行失败=1；
- down：默认保卷直接 down；--volumes 必须显式确认（拒绝=不执行删卷，确认后才带 -v）；
- 批次 B 占位：migrate/config/capability/log → 明确「未实装」提示 + 退出码 2。
退出码语义权威=services/cli/oadm.py 模块 docstring（对齐 services/cli/oa.py：0/1/2）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner, Result
from pydantic import BaseModel, Field, ValidationError

from services.cli import oadm

# --------------------------------------------------------------- 夹具与小工具


def _invoke(*args: str, **kwargs: Any) -> Result:
    return CliRunner().invoke(oadm.cli, list(args), **kwargs)


def _all_output(result: Result) -> str:
    """stdout + stderr 合并（兼容 click 8.1 混合流与 8.2+ 分离流两种 Result 行为）。"""
    out = result.output
    try:
        out += result.stderr
    except ValueError:
        pass
    return out


def _green(name: str) -> oadm.CheckResult:
    return oadm.CheckResult(name, True, "桩：绿")


def _red(name: str, fix: str = "修复指引桩") -> oadm.CheckResult:
    return oadm.CheckResult(name, False, "桩：红", fix)


def _patch_doctor_all(monkeypatch: pytest.MonkeyPatch, maker) -> None:
    """把 doctor 八项检查函数全部替换为桩工厂产物。"""
    targets = (
        "_check_python",
        "_check_uv",
        "_check_disk",
        "_check_ports",
        "_check_storage",
        "_check_migration",
        "_check_frontend_dist",
        "_check_env_profile",
    )
    for target in targets:
        monkeypatch.setattr(oadm, target, lambda t=target: maker(t))


def _completed(cmd: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, returncode=returncode, stdout=stdout, stderr=stderr)


def _storage_stub(monkeypatch: pytest.MonkeyPatch, healthy: bool) -> None:
    """_wait_storage_healthy 桩：统一全绿/全红两态。"""
    monkeypatch.setattr(
        oadm,
        "_wait_storage_healthy",
        lambda timeout_s=90.0, interval_s=3.0: {name: healthy for name in oadm._STORAGE_CONTAINERS},
    )


@pytest.fixture()
def repo_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 REPO_ROOT 指到临时目录（compose 文件按需落位；.env 缺省不存在）。"""
    monkeypatch.setattr(oadm, "REPO_ROOT", tmp_path)
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    return tmp_path


def _seed_compose_files(root: Path, names: tuple[str, ...]) -> None:
    deploy = root / "deploy"
    deploy.mkdir(exist_ok=True)
    for name in names:
        (deploy / name).write_text(f"# 桩 compose：{name}\n", encoding="utf-8")


# --------------------------------------------------------------- 命令树装配与批次 B 占位


def test_命令树八命令在册() -> None:
    # Arrange / Act
    result = _invoke("--help")
    # Assert：ops/04 §5 双命令树运维域——四件实装 + 四件批次 B 占位
    assert result.exit_code == 0, _all_output(result)
    for name in ("doctor", "status", "up", "down", "migrate", "config", "capability", "log"):
        assert name in result.output, f"命令 {name} 不在命令树"


def test_批次B占位命令_提示未实装且退出码2() -> None:
    for name in ("migrate", "config", "capability", "log"):
        # Arrange / Act
        result = _invoke(name)
        # Assert：占位=「本机前置不满足」族退出码 2，且明示批次 B 归属（ops/04 §7）
        assert result.exit_code == 2, f"{name}: {_all_output(result)}"
        out = _all_output(result)
        assert "未实装" in out and "批次 B" in out


# --------------------------------------------------------------- doctor：八项判定与门禁退出码


def test_doctor_八项顺序固定() -> None:
    # Arrange / Act（直调检查装配面，次序即 ops/04 §3.1 前置自检口径）
    results = oadm.doctor_checks()
    # Assert
    assert [r.name for r in results] == [
        "Python ≥3.11",
        "uv",
        "磁盘 ≥2GiB",
        "端口占用(8000/5432/6379/9000)",
        "存储容器健康",
        "迁移版本",
        "前端 dist",
        "配置档解析",
    ]


def test_doctor_全绿退出0(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    _patch_doctor_all(monkeypatch, lambda name: _green(name))
    # Act
    result = _invoke("doctor")
    # Assert
    assert result.exit_code == 0, _all_output(result)
    assert "8/8 全绿" in _all_output(result)


def test_doctor_任一红退出1并渲染修复指引(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：七绿一红（uv 缺失为安装期最常见红项；maker 收到的是检查函数名桩）
    def maker(target: str) -> oadm.CheckResult:
        if target == "_check_uv":
            return _red("uv", fix="官方自举：curl -LsSf https://astral.sh/uv/install.sh | sh")
        return _green(target.removeprefix("_check_"))

    _patch_doctor_all(monkeypatch, maker)
    # Act
    result = _invoke("doctor")
    # Assert：门禁语义=1（install.sh 第 7 步据此拦截）
    assert result.exit_code == 1, _all_output(result)
    out = _all_output(result)
    assert "✗ uv" in out and "修复: 官方自举" in out
    assert "1 红需处理后重跑" in out


def test_doctor_python版本判定(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：主 venv 即 ≥3.11；断言真实判定走通（不依赖桩）
    monkeypatch.setattr(oadm.shutil, "which", lambda _: None)  # 顺带把 uv 项压红，聚焦不混判
    results = {r.name: r for r in oadm.doctor_checks()}
    # Assert
    assert results["Python ≥3.11"].ok is True
    assert results["uv"].ok is False and "astral.sh" in results["uv"].fix


def test_doctor_磁盘不足判红(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：free=1GiB < 2GiB 阈值（disk_usage 只消费 .free，SimpleNamespace 桩即可）
    monkeypatch.setattr(
        oadm.shutil, "disk_usage", lambda _p: SimpleNamespace(total=4 << 30, used=3 << 30, free=1 << 30)
    )
    # Act
    result = oadm._check_disk()
    # Assert
    assert result.ok is False and "清理磁盘" in result.fix


def test_doctor_磁盘充足判绿() -> None:
    # Act
    result = oadm._check_disk()
    # Assert：真实盘 ≥2GiB（开发机常态；不满足时该用例失败=环境问题显性化）
    assert result.ok is True, result.detail


def test_doctor_端口外部占用判红_平台容器占用让行(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：全端口被占；docker ps 返回三件存储 healthy（api 不在）
    monkeypatch.setattr(oadm, "_port_in_use", lambda _port: True)
    monkeypatch.setattr(
        oadm,
        "_docker_ps",
        lambda: {
            "onto-agent-postgres-1": "Up 3 minutes (healthy)",
            "onto-agent-redis-1": "Up 3 minutes (healthy)",
            "onto-agent-minio-1": "Up 3 minutes (healthy)",
        },
    )
    # Act
    result = oadm._check_ports()
    # Assert：本平台 healthy 容器占用=让行（幂等重装，ops/04 §6 验收 3）；
    # api 容器不在场 → 8000 仍判红 → 整项红（混合明细）
    assert result.ok is False, result.detail
    assert "让行" in result.detail
    assert "8000 被外部进程占用" in result.detail

    # Arrange 2：无任何平台容器 → 全部红
    monkeypatch.setattr(oadm, "_docker_ps", lambda: {})
    result2 = oadm._check_ports()
    # Assert 2
    assert result2.ok is False and "外部进程占用" in result2.detail and "端口映射" in result2.fix


def test_doctor_存储容器缺判红(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：redis 缺席
    monkeypatch.setattr(
        oadm,
        "_docker_ps",
        lambda: {"onto-agent-postgres-1": "Up (healthy)", "onto-agent-minio-1": "Up (healthy)"},
    )
    # Act
    result = oadm._check_storage()
    # Assert
    assert result.ok is False and "onto-agent-redis-1 缺失" in result.detail and "oadm up" in result.fix

    # Arrange 2：三件全 healthy → 绿
    monkeypatch.setattr(
        oadm,
        "_docker_ps",
        lambda: {name: "Up 1 minute (healthy)" for name in oadm._STORAGE_CONTAINERS},
    )
    result2 = oadm._check_storage()
    # Assert 2
    assert result2.ok is True


def test_doctor_迁移落后与未迁移判红(monkeypatch: pytest.MonkeyPatch, repo_tmp: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
        calls.append(cmd)
        if cmd[-1] == "current":
            return _completed(cmd, 0, stdout="20260927_f0f79f84dce4 (head)\n")
        return _completed(cmd, 0, stdout="20260927_f0f79f84dce4 (head)\n")

    # Arrange：current=head → 绿
    monkeypatch.setattr(oadm, "_run", fake_run)
    result = oadm._check_migration()
    # Assert
    assert result.ok is True and "head" in result.detail
    assert all("alembic" in " ".join(c) for c in calls)

    # Arrange：尚未迁移（current 空输出）
    monkeypatch.setattr(
        oadm,
        "_run",
        lambda cmd, timeout=120.0: _completed(cmd, 0, stdout="" if cmd[-1] == "current" else "abc (head)\n"),
    )
    result2 = oadm._check_migration()
    # Assert 2
    assert result2.ok is False and "尚未迁移" in result2.detail

    # Arrange：alembic 执行失败（PG 连不通族，诊断走 stderr）
    monkeypatch.setattr(
        oadm,
        "_run",
        lambda cmd, timeout=120.0: _completed(cmd, 1, stderr="could not connect to server"),
    )
    result3 = oadm._check_migration()
    # Assert 3：修复指引含 PG 连通诊断
    assert result3.ok is False and "PG" in result3.fix and "could not connect" in result3.detail

    # Arrange 4：失败摘要写 stdout 的实况形态（真机实证：alembic 的 FAILED 行在 stdout，
    # 如共享库被并行会话迁移到未合入修订 → "Can't locate revision"）
    monkeypatch.setattr(
        oadm,
        "_run",
        lambda cmd, timeout=120.0: _completed(cmd, 1, stdout="FAILED: Can't locate revision identified by 'x'\n"),
    )
    result4 = oadm._check_migration()
    # Assert 4：stdout 诊断同样透出（不再「无输出」）
    assert result4.ok is False and "Can't locate revision" in result4.detail and "迁移漂移" in result4.fix


def test_doctor_前端dist在场判定(monkeypatch: pytest.MonkeyPatch, repo_tmp: Path) -> None:
    # Arrange：dist 缺失
    result = oadm._check_frontend_dist()
    # Assert：零 npm 口径——发布包自带产物，缺失判红并给两条出路
    assert result.ok is False and "零 npm" in result.fix and "API-only" in result.fix

    # Arrange 2：产物在场
    dist = repo_tmp / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    result2 = oadm._check_frontend_dist()
    # Assert 2
    assert result2.ok is True


def test_doctor_配置档解析判定(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：非法档位值 → ValidationError
    class _Probe(BaseModel):
        deploy_profile: str = Field(default="lite", pattern="^(lite|full)$")

    try:
        _Probe(deploy_profile="full-cad")  # type: ignore[arg-type]
        raised = None
    except ValidationError as exc:
        raised = exc
    assert raised is not None
    monkeypatch.setattr(oadm, "_load_settings", lambda: (_ for _ in ()).throw(raised))
    result = oadm._check_env_profile()
    # Assert
    assert result.ok is False and "校验失败" in result.detail and "模板" in result.fix

    # Arrange 2：解析通过 → 绿 + 档位透出
    monkeypatch.setattr(oadm, "_load_settings", lambda: _FakeSettings(deploy_profile="lite"))
    result2 = oadm._check_env_profile()
    # Assert 2
    assert result2.ok is True and "lite" in result2.detail


class _FakeSettings:
    """Settings 形状桩（避免测试间 env 污染；仅暴露被读字段）。"""

    def __init__(self, deploy_profile: str = "lite", llm_base_url: str | None = None, llm_model: str = "m") -> None:
        self.deploy_profile = deploy_profile
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.ollama_base_url = "http://embed.test/"
        self.embed_protocol = "tei"


def test_doctor_配置档解析真实Settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：进程 env 注入合法档位（唯一取配置入口=services/platform/config.py）
    monkeypatch.setenv("OA_DEPLOY_PROFILE", "full")
    monkeypatch.delenv("OA_LLM_TIMEOUT_S", raising=False)
    # Act
    result = oadm._check_env_profile()
    # Assert
    assert result.ok is True and "full" in result.detail

    # Arrange 2：env 注入越界值（llm_timeout_s 下界 10）→ 红
    monkeypatch.setenv("OA_LLM_TIMEOUT_S", "5")
    result2 = oadm._check_env_profile()
    # Assert 2
    assert result2.ok is False and "校验失败" in result2.detail


def test_status_llm未配置判红_可达性探活(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：未配置
    monkeypatch.setattr(oadm, "_load_settings", lambda: _FakeSettings(llm_base_url=None))
    result = oadm._check_llm()
    # Assert：未配置=红（健康矩阵透出「当前档缺什么」，ops/04 §4）
    assert result.ok is False and "OA_LLM_BASE_URL" in result.fix

    # Arrange 2：已配置 + 探活成功（GET {base}/models）
    probed: list[str] = []

    def fake_probe(url: str) -> tuple[bool, str]:
        probed.append(url)
        return True, "HTTP 200"

    monkeypatch.setattr(oadm, "_load_settings", lambda: _FakeSettings(llm_base_url="http://llm.test/v1"))
    monkeypatch.setattr(oadm, "_http_probe", fake_probe)
    result2 = oadm._check_llm()
    # Assert 2
    assert result2.ok is True and probed == ["http://llm.test/v1/models"]

    # Arrange 3：不可达 → 红
    monkeypatch.setattr(oadm, "_http_probe", lambda _url: (False, "不可达（ConnectError）"))
    result3 = oadm._check_llm()
    # Assert 3
    assert result3.ok is False and "核对端点" in result3.fix


def test_status_嵌入端点按协议探活(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：tei 协议 → /health；ollama 协议 → /api/tags
    probed: list[str] = []

    def fake_probe(url: str) -> tuple[bool, str]:
        probed.append(url)
        return True, "HTTP 200"

    monkeypatch.setattr(oadm, "_http_probe", fake_probe)
    settings = _FakeSettings()  # embed_protocol="tei"、base 带 尾斜杠
    monkeypatch.setattr(oadm, "_load_settings", lambda: settings)
    result = oadm._check_embed()
    # Assert
    assert result.ok is True and probed == ["http://embed.test/health"]

    settings.embed_protocol = "ollama"
    result2 = oadm._check_embed()
    # Assert 2
    assert result2.ok is True and probed[-1] == "http://embed.test/api/tags"


# --------------------------------------------------------------- status：矩阵与 json 形状


def _patch_status(monkeypatch: pytest.MonkeyPatch, red_names: frozenset[str] = frozenset()) -> None:
    def maker(name: str) -> oadm.CheckResult:
        return _red(name) if name in red_names else _green(name)

    for target, name in (
        ("_check_storage", "存储容器健康"),
        ("_check_migration", "迁移版本"),
        ("_check_llm", "LLM 端点"),
        ("_check_embed", "嵌入端点"),
        ("_check_frontend_dist", "前端 dist"),
    ):
        monkeypatch.setattr(oadm, target, lambda n=name: maker(n))


def test_status_json形状_全绿(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    _patch_status(monkeypatch)
    # Act
    result = _invoke("status", "--output", "json")
    # Assert：ops/04 §6 验收 5 的 CI 消费契约——顶层 items/all_ok，元素四字段
    assert result.exit_code == 0, _all_output(result)
    data = json.loads(result.output)
    assert set(data) == {"items", "all_ok"}
    assert data["all_ok"] is True
    assert [i["name"] for i in data["items"]] == ["存储容器健康", "迁移版本", "LLM 端点", "嵌入端点", "前端 dist"]
    assert all(set(i) == {"name", "ok", "detail", "fix"} for i in data["items"])


def test_status_json形状_有红项退出1(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    _patch_status(monkeypatch, red_names=frozenset({"LLM 端点"}))
    # Act
    result = _invoke("status", "--output", "json")
    # Assert：健康矩阵门禁语义=任一红退出 1（CI 判栈健康）
    assert result.exit_code == 1, _all_output(result)
    data = json.loads(result.output)
    assert data["all_ok"] is False
    assert next(i for i in data["items"] if i["name"] == "LLM 端点")["ok"] is False


def test_status_文本渲染五行(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    _patch_status(monkeypatch, red_names=frozenset({"前端 dist"}))
    # Act
    result = _invoke("status")
    # Assert
    out = _all_output(result)
    assert result.exit_code == 1, out
    for name in ("存储容器健康", "迁移版本", "LLM 端点", "嵌入端点", "前端 dist"):
        assert name in out
    assert "存在红项" in out and "修复: 修复指引桩" in out


# --------------------------------------------------------------- up：compose 组合与幂等


@pytest.fixture()
def up_stub(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """捕获 _run 入参并默认成功；返回捕获列表供断言。"""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return _completed(cmd, 0, stdout="")

    monkeypatch.setattr(oadm, "_run", fake_run)
    _storage_stub(monkeypatch, healthy=True)
    return calls


def test_up_lite_组合base加local无profile(
    monkeypatch: pytest.MonkeyPatch, repo_tmp: Path, up_stub: list[list[str]]
) -> None:
    # Arrange
    _seed_compose_files(repo_tmp, ("docker-compose.yml", "docker-compose.local.yml"))
    # Act
    result = _invoke("up")
    # Assert
    assert result.exit_code == 0, _all_output(result)
    cmd = up_stub[0]
    assert cmd.count("-f") == 2
    assert str(repo_tmp / "deploy" / "docker-compose.yml") in cmd
    assert str(repo_tmp / "deploy" / "docker-compose.local.yml") in cmd
    assert "--profile" not in cmd
    assert cmd[-3:] == ["up", "-d", "--remove-orphans"]  # 幂等口径
    assert "✓ onto-agent-postgres-1" in _all_output(result)


def test_up_full档追加全栈骨架与profile(
    monkeypatch: pytest.MonkeyPatch, repo_tmp: Path, up_stub: list[list[str]]
) -> None:
    # Arrange
    _seed_compose_files(repo_tmp, ("docker-compose.yml", "docker-compose.local.yml", "docker-compose.full.yml"))
    # Act
    result = _invoke("up", "--profile", "full")
    # Assert
    assert result.exit_code == 0, _all_output(result)
    cmd = up_stub[0]
    assert cmd.count("-f") == 3
    assert str(repo_tmp / "deploy" / "docker-compose.full.yml") in cmd
    assert "--profile" in cmd and "full" in cmd
    assert "full-cad" not in cmd

    # Act 2：full-cad 同形
    result2 = _invoke("up", "--profile", "full-cad")
    # Assert 2
    assert result2.exit_code == 0, _all_output(result2)
    assert "full-cad" in up_stub[1]


def test_up_env档存在时注入(monkeypatch: pytest.MonkeyPatch, repo_tmp: Path, up_stub: list[list[str]]) -> None:
    # Arrange
    _seed_compose_files(repo_tmp, ("docker-compose.yml", "docker-compose.local.yml"))
    (repo_tmp / ".env").write_text("OA_JWT_SECRET=x\n", encoding="utf-8")
    # Act
    result = _invoke("up")
    # Assert：compose --env-file 注入既有 .env（存储栈同名变量对齐）
    assert result.exit_code == 0, _all_output(result)
    cmd = up_stub[0]
    assert "--env-file" in cmd and str(repo_tmp / ".env") in cmd


def test_up_compose文件缺失_用法错误2(
    monkeypatch: pytest.MonkeyPatch, repo_tmp: Path, up_stub: list[list[str]]
) -> None:
    # Arrange：只给 base（full 档缺全栈骨架文件）
    _seed_compose_files(repo_tmp, ("docker-compose.yml", "docker-compose.local.yml"))
    # Act
    result = _invoke("up", "--profile", "full")
    # Assert：本机前置不满足=2
    assert result.exit_code == 2, _all_output(result)
    assert "缺失" in _all_output(result)
    assert up_stub == [], "前置缺失不得发起 compose 调用"


def test_up_docker缺失_用法错误2(monkeypatch: pytest.MonkeyPatch, repo_tmp: Path, up_stub: list[list[str]]) -> None:
    # Arrange
    _seed_compose_files(repo_tmp, ("docker-compose.yml", "docker-compose.local.yml"))
    monkeypatch.setattr(oadm.shutil, "which", lambda _: None)
    # Act
    result = _invoke("up")
    # Assert
    assert result.exit_code == 2, _all_output(result)
    assert "docker" in _all_output(result)


def test_up_compose失败退出1(monkeypatch: pytest.MonkeyPatch, repo_tmp: Path) -> None:
    # Arrange：compose 执行失败
    _seed_compose_files(repo_tmp, ("docker-compose.yml", "docker-compose.local.yml"))
    monkeypatch.setattr(oadm, "_run", lambda cmd, timeout=120.0: _completed(cmd, 1, stderr="pull access denied"))
    # Act
    result = _invoke("up")
    # Assert：执行失败=1（业务/健康失败族）
    assert result.exit_code == 1, _all_output(result)
    assert "docker compose up 失败" in _all_output(result)


def test_up_存储未按时转绿退出1(monkeypatch: pytest.MonkeyPatch, repo_tmp: Path) -> None:
    # Arrange
    _seed_compose_files(repo_tmp, ("docker-compose.yml", "docker-compose.local.yml"))
    monkeypatch.setattr(oadm, "_run", lambda cmd, timeout=120.0: _completed(cmd, 0))
    _storage_stub(monkeypatch, healthy=False)
    # Act
    result = _invoke("up")
    # Assert
    assert result.exit_code == 1, _all_output(result)
    assert "oadm status 复核" in _all_output(result)


def test_wait_storage_healthy_全绿不轮询睡眠(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：首探即全绿
    monkeypatch.setattr(
        oadm, "_docker_ps", lambda: {name: "Up 2 minutes (healthy)" for name in oadm._STORAGE_CONTAINERS}
    )

    def _no_sleep(_s: float) -> None:
        raise AssertionError("全绿时不得进入轮询睡眠")

    monkeypatch.setattr(oadm.time, "sleep", _no_sleep)
    # Act
    states = oadm._wait_storage_healthy(timeout_s=30.0, interval_s=1.0)
    # Assert
    assert states == {name: True for name in oadm._STORAGE_CONTAINERS}


def test_wait_storage_healthy_超时即返回(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：永不全绿 + timeout=0（单探即达死线，无睡眠）
    monkeypatch.setattr(oadm, "_docker_ps", lambda: {})

    def _no_sleep(_s: float) -> None:
        raise AssertionError("超时路径不得进入睡眠")

    monkeypatch.setattr(oadm.time, "sleep", _no_sleep)
    # Act
    states = oadm._wait_storage_healthy(timeout_s=0.0, interval_s=1.0)
    # Assert
    assert states == {name: False for name in oadm._STORAGE_CONTAINERS}


# --------------------------------------------------------------- down：保卷默认与 --volumes 显式确认


def test_down_默认保卷直接执行(repo_tmp: Path, up_stub: list[list[str]]) -> None:
    # Arrange：down 用 base+local+full 三文件全量（覆盖 full 起的 api）
    _seed_compose_files(repo_tmp, ("docker-compose.yml", "docker-compose.local.yml", "docker-compose.full.yml"))
    # Act
    result = _invoke("down")
    # Assert：不带 -v，不触发确认
    assert result.exit_code == 0, _all_output(result)
    cmd = up_stub[0]
    assert cmd[-1] == "down" and "-v" not in cmd
    assert "数据卷保留" in _all_output(result)


def test_down_volumes_拒绝则不执行(repo_tmp: Path, up_stub: list[list[str]]) -> None:
    # Arrange：compose 文件在位（否则会先于确认短路退出，测不到确认语义）
    _seed_compose_files(repo_tmp, ("docker-compose.yml", "docker-compose.local.yml", "docker-compose.full.yml"))
    # Act：显式确认问句回答 n
    result = _invoke("down", "--volumes", input="n\n")
    # Assert：走到了确认问句并被拒绝=中止（click Abort 退出码 1），绝不发起删卷调用
    out = _all_output(result)
    assert "将删除数据卷" in out, out  # 确认问句确实发出
    assert result.exit_code != 0, out
    assert up_stub == [], "拒绝确认后不得执行任何 compose down"
    assert "数据卷保留" not in out


def test_down_volumes_确认后带_v(repo_tmp: Path, up_stub: list[list[str]]) -> None:
    # Arrange
    _seed_compose_files(repo_tmp, ("docker-compose.yml", "docker-compose.local.yml", "docker-compose.full.yml"))
    # Act：确认 y
    result = _invoke("down", "--volumes", input="y\n")
    # Assert：确认后才删卷（-v），并提示 ~/.oa 凭据需手动清理（ops/04 §6 验收 4 口径）
    assert result.exit_code == 0, _all_output(result)
    cmd = up_stub[0]
    assert cmd[-2:] == ["down", "-v"]
    assert "含数据卷" in _all_output(result) and "~/.oa" in _all_output(result)


def test_up_profile越界_用法错误2(repo_tmp: Path, up_stub: list[list[str]]) -> None:
    # Arrange / Act
    result = _invoke("up", "--profile", "gpu")
    # Assert：Click choice 越界=2（参数越界族）
    assert result.exit_code == 2, _all_output(result)
    assert up_stub == []
