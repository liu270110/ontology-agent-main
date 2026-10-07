"""安装器与三档配置模板测试（bash 函数库单测 + PS 语法门 + 模板键纪律）。

覆盖（standards/01 §2.9：AAA 布局、中文命名）：
- install.sh：bash -n 语法检；source 后直调关键函数（oa_env_synth 合成 / oa_env_place
  首配不覆盖 / oa_port_busy 端口探测 / oa_container_running 容器让行判定）——
  「可 source 函数库形态」的验收面；
- bin/oadm：bash -n 语法检；
- install.ps1：PowerShell Parser 语法检（powershell 缺失自动 skip）+ UTF-8 BOM 存在性
  （PS 5.1 无 BOM 按 ANSI 误读中文注释）；
- 三档模板（ops/04 §4 + standards/03 §3 矩阵）：活跃（非注释）OA_* 键必须全部存在于
  统一配置层 Settings（「不新增配置键，只用既有 OA_*」纪律）；三档档位键与矩阵默认能力
  集口径；安装器合成段追加=dotenv 末次生效（第 6 步覆盖语义实测）。

平台依赖：bash（Git for Windows/msys）与 powershell 缺失时对应用例 skip，不判红。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import dotenv
import pytest

from services.platform.config import Settings

REPO = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO / "install.sh"
INSTALL_PS1 = REPO / "install.ps1"


def _find_bash() -> str | None:
    """定位可用的 bash：Windows 上 CreateProcess 对裸名 bash 的搜索会先命中
    C:\\Windows\\System32\\bash.exe（WSL，跑不了 Windows 路径，报 Bash/Service/E_UNEXPECTED），
    故显式优先 Git for Windows 的 bin/bash（经 git.exe 推导），逐个探活后使用。"""
    candidates: list[str] = []
    override = os.environ.get("OA_TEST_BASH")
    if override:
        candidates.append(override)
    git_exe = shutil.which("git")
    if git_exe:
        git_root = Path(git_exe).resolve().parents[1]
        candidates += [str(git_root / "bin" / "bash.exe"), str(git_root / "usr" / "bin" / "bash.exe")]
    which_bash = shutil.which("bash")
    if which_bash:
        candidates.append(which_bash)
    for cand in candidates:
        if cand and Path(cand).is_file():
            try:
                probe = subprocess.run([cand, "-c", "echo ok"], capture_output=True, timeout=15, check=False)
            except OSError:
                continue
            if probe.returncode == 0 and b"ok" in probe.stdout:
                return cand
    return None


BASH = _find_bash()
requires_bash = pytest.mark.skipif(BASH is None, reason="无可用 bash（Git for Windows 缺失且未设 OA_TEST_BASH）")
requires_pwsh = pytest.mark.skipif(shutil.which("powershell") is None, reason="无 powershell")


def _bash(script: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """跑 bash 片段（cwd=worktree 根，source install.sh 后直调函数）。"""
    assert BASH is not None
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO,
        env=env,
        timeout=120,
        check=False,
    )


# --------------------------------------------------------------- 语法检（bash -n / PS Parser）


@requires_bash
def test_install_sh与bin_oadm_语法检() -> None:
    # Act：bash -n 只做语法解析不执行
    assert BASH is not None
    for target in (INSTALL_SH, REPO / "bin" / "oadm"):
        proc = subprocess.run(
            [BASH, "-n", str(target)], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
        # Assert
        assert proc.returncode == 0, f"{target}: {proc.stderr}"


@requires_pwsh
def test_install_ps1_语法检() -> None:
    # Arrange / Act：PowerShell 语言 Parser 全文解析（零执行）
    script = (
        "$t=$null;$e=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile"
        "('INSTALL_PS1',[ref]$t,[ref]$e)|Out-Null;"
        "if($e.Count -gt 0){$e|ForEach-Object{$_.Message};exit 1}else{exit 0}"
    ).replace("INSTALL_PS1", str(INSTALL_PS1).replace("'", "''"))
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, check=False)
    # Assert
    assert proc.returncode == 0, proc.stdout + proc.stderr


@requires_pwsh
def test_install_ps1_关键函数_点源调用() -> None:
    """点源 install.ps1 后直调 Oa-* 函数（ocr 评审 #7：与 bash 侧函数单测对齐的 PS 面）。

    覆盖：ConvertTo-OaEnvSynth 合成段（本地渠道 URL/合成段标记）、Publish-OaEnv
    写入（written）与备份保留（kept，首配不覆盖）双态——$Root 重定向到临时目录，
    绝不触碰真实仓库 .env。
    """
    script = r"""
$ErrorActionPreference = 'Stop'
. 'INSTALL_PS1'
$real = $Root
$d = Join-Path $env:TEMP ("oa-ps-test-{0}" -f [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $d | Out-Null
try {
    Copy-Item (Join-Path $real '.env.lite') (Join-Path $d '.env.lite')
    $Root = $d                       # 函数的仓库根重定向到临时目录
    $synth = ConvertTo-OaEnvSynth 'lite' 'local' 'none'
    if ($synth -notmatch '127\.0\.0\.1:18001/v1') { Write-Error 'synth 缺本地 vLLM 渠道'; exit 1 }
    if ($synth -notmatch '安装器合成段') { Write-Error 'synth 缺合成段标记'; exit 1 }
    $synthPath = Join-Path $d 'synth.env'
    [System.IO.File]::WriteAllText($synthPath, $synth, [System.Text.UTF8Encoding]::new($false))
    $w1 = Publish-OaEnv $synthPath
    if ($w1 -ne 'written') { Write-Error "首落位期望 written，实际 $w1"; exit 1 }
    if (-not (Test-Path (Join-Path $d '.env'))) { Write-Error '.env 未写入'; exit 1 }
    $w2 = Publish-OaEnv $synthPath
    if ($w2 -ne 'kept') { Write-Error "重落位期望 kept（首配不覆盖），实际 $w2"; exit 1 }
    $bak = @(Get-ChildItem (Join-Path $d '.env.bak-*'))
    if ($bak.Count -lt 1) { Write-Error '备份 .env.bak-* 未生成'; exit 1 }
    Write-Output 'PS_FUNCS_OK'
} finally {
    Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue
}
""".replace("INSTALL_PS1", str(INSTALL_PS1).replace("'", "''"))
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )
    # Assert：函数链走通（合成→写入→备份保留），零落真实仓库
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PS_FUNCS_OK" in proc.stdout


def test_install_ps1_utf8_bom在位() -> None:
    # Act / Assert：PS 5.1 中文注释依赖 BOM（deploy/README 行尾纪律条目）
    assert INSTALL_PS1.read_bytes()[:3] == b"\xef\xbb\xbf", "install.ps1 必须 UTF-8 带 BOM"


# --------------------------------------------------------------- install.sh 关键函数单测（可 source 形态）


@requires_bash
def test_oa_env_synth_云渠道与ollama合成() -> None:
    # Act
    proc = _bash("source install.sh && oa_env_synth lite cloud ollama")
    # Assert：模板全量 + 合成段追加（三问结果，ops/04 §3.1 第 6 步）
    assert proc.returncode == 0, proc.stderr
    assert "OA_LLM_BASE_URL=https://api.deepseek.com" in proc.stdout
    assert "OA_DEPLOY_PROFILE=lite" in proc.stdout
    assert "# ===== 安装器合成段" in proc.stdout
    assert "OA_OLLAMA_BASE_URL=http://localhost:11434" in proc.stdout
    assert "OA_EMBED_PROTOCOL=ollama" in proc.stdout


@requires_bash
def test_oa_env_synth_本地渠道与tei合成() -> None:
    # Act：full 档 + 本地 vLLM + TEI（GPU 通道口径）
    proc = _bash("source install.sh && oa_env_synth full local tei")
    # Assert
    assert proc.returncode == 0, proc.stderr
    assert "OA_LLM_BASE_URL=http://127.0.0.1:18001/v1" in proc.stdout
    assert "OA_LLM_MODEL=local-main" in proc.stdout
    assert "OA_EMBED_PROTOCOL=tei" in proc.stdout


@requires_bash
def test_oa_env_synth_模板缺失报错() -> None:
    # Act：不存在档位 → 函数失败（rc=1），main 层据此 oa_die
    proc = _bash("source install.sh && oa_env_synth gpu none none")
    # Assert
    assert proc.returncode != 0 and "模板缺失" in proc.stderr


@requires_bash
def test_oa_env_place_新档写入() -> None:
    # Act：无既有 .env → 直接写入（rc=0；`|| rc=$?` 承接——source 带 set -e 的函数调用纪律）
    proc = _bash(
        'source install.sh && d="$(mktemp -d)" && printf "OA_A=1\\n" > "$d/synth.env" && place_rc=0 '
        '&& { oa_env_place "$d" "$d/synth.env" || place_rc=$?; }; '
        'printf "RC=%s\\n" "$place_rc"; cat "$d/.env"; rm -rf "$d"'
    )
    # Assert
    assert proc.returncode == 0, proc.stderr
    assert "RC=0" in proc.stdout and "OA_A=1" in proc.stdout
    assert "已写入" in proc.stdout


@requires_bash
def test_oa_env_place_已有档备份不覆盖() -> None:
    # Act：既有 .env → 备份 .env.bak-<ts>，原档保留（rc=2=正常「保留」路径；
    # source install.sh 带 set -e，非零返回须经 `|| rc=$?` 捕获，与 main() 内用法一致）
    proc = _bash(
        'source install.sh && d="$(mktemp -d)" && printf "OA_KEEP=original\\n" > "$d/.env" && '
        'printf "OA_NEW=1\\n" > "$d/synth.env" && place_rc=0 && { oa_env_place "$d" "$d/synth.env" || place_rc=$?; }; '
        'printf "RC=%s\\n" "$place_rc"; printf "MAIN=%s\\n" "$(cat "$d/.env")"; ls "$d"/.env.bak-*; rm -rf "$d"'
    )
    # Assert：首配不覆盖（ops/04 §4）——原档内容原样、备份文件在位、diff 提示输出
    assert proc.returncode == 0, proc.stderr
    assert "RC=2" in proc.stdout
    assert "MAIN=OA_KEEP=original" in proc.stdout
    assert ".env.bak-" in proc.stdout
    assert "绝不覆盖" in proc.stdout or "不覆盖" in proc.stdout
    assert "OA_KEEP=original" in proc.stdout  # diff -u 输出含原档行


@requires_bash
def test_oa_env_place_diff_密钥值脱敏() -> None:
    # Arrange：既有 .env 含用户已填密钥值（合成档同键为空 → diff 必回显该行）
    proc = _bash(
        'source install.sh && d="$(mktemp -d)" && '
        'printf "OA_LLM_API_KEY=real-secret-value\\nOA_JWT_SECRET=top-secret\\n" > "$d/.env" && '
        'printf "OA_LLM_API_KEY=\\nOA_JWT_SECRET=\\n" > "$d/synth.env" && '
        'place_rc=0 && { oa_env_place "$d" "$d/synth.env" || place_rc=$?; }; '
        'printf "RC=%s\\n" "$place_rc"; rm -rf "$d"'
    )
    # Assert：diff 回显密钥值脱敏（ocr 评审 #9）；原档保留路径 rc=2 不受影响
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "RC=2" in out
    assert "******" in out
    assert "real-secret-value" not in out and "top-secret" not in out


@requires_bash
def test_oa_port_busy_探测监听与空闲() -> None:
    # Arrange：Python 侧领一个空闲端口，bash 侧起监听后探测（监听器由 python 子进程承载）
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    script = """
source install.sh
"$PY" -c "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',$PORT)); s.listen(1); time.sleep(20)" &
LP=$!
sleep 1
if oa_port_busy "$PORT"; then echo BUSY_OK; else echo BUSY_FAIL; fi
kill "$LP" 2>/dev/null
sleep 0.5
if oa_port_busy "$PORT"; then echo FREE_FAIL; else echo FREE_OK; fi
"""
    # Act
    proc = _bash(script, env_extra={"PORT": str(port), "PY": sys.executable.replace("\\", "/")})
    # Assert
    assert proc.returncode == 0, proc.stderr
    assert "BUSY_OK" in proc.stdout and "FREE_OK" in proc.stdout
    assert "BUSY_FAIL" not in proc.stdout and "FREE_FAIL" not in proc.stdout


@requires_bash
def test_oa_container_running_healthy判定经PATH桩() -> None:
    # Arrange：PATH 桩替换 docker——按过滤参数分流（postgres→healthy；redis→无 healthy）
    script = (
        'SHIM="$(mktemp -d)"; '
        "cat > \"$SHIM/docker\" <<'EOF'\n"
        "#!/usr/bin/env bash\n"
        'if printf "%s\\n" "$*" | grep -q "name=onto-agent-postgres"; then printf "Up 5 minutes (healthy)\\n"; '
        'else printf "Up 5 minutes\\n"; fi\n'
        "EOF\n"
        'chmod +x "$SHIM/docker"; '
        "source install.sh; "
        'PATH="$SHIM:$PATH"; '
        "if oa_container_running postgres; then echo PG_HEALTHY_OK; else echo PG_HEALTHY_FAIL; fi; "
        "if oa_container_running redis; then echo REDIS_HEALTHY_FAIL; else echo REDIS_HEALTHY_OK; fi; "
        'rm -rf "$SHIM"'
    )
    # Act
    proc = _bash(script)
    # Assert：postgres=healthy 判在场；redis 桩输出无 healthy 判不在场
    assert proc.returncode == 0, proc.stderr
    assert "PG_HEALTHY_OK" in proc.stdout and "REDIS_HEALTHY_OK" in proc.stdout
    assert "FAIL" not in proc.stdout


# --------------------------------------------------------------- 三档模板纪律（只用既有 OA_* 键）


def _template_active_keys(name: str) -> dict[str, str]:
    values = dotenv.dotenv_values(REPO / name)
    return {k: v for k, v in values.items() if v is not None}


@pytest.mark.parametrize("name", [".env.lite", ".env.full", ".env.full-cad"])
def test_三档模板活跃键全部存在于统一配置层(name: str) -> None:
    # Arrange：统一配置层的 env 键全集（字段名 → OA_ 前缀环境键；无 alias，env 名=OA_+字段名大写）
    settings_env_keys = {f"OA_{field.upper()}" for field in Settings.model_fields}
    # Act：dotenv 只取未注释键
    keys = _template_active_keys(name)
    # Assert：「不新增配置键，只用既有 OA_*」纪律（任务书；唯一取配置入口=services/platform/config.py）
    assert keys, f"{name} 无活跃键（模板损坏？）"
    unknown = set(keys) - settings_env_keys
    assert not unknown, f"{name} 含统一配置层不存在的键：{sorted(unknown)}"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (".env.lite", {"OA_DEPLOY_PROFILE": "lite", "OA_KB_PARSER": "pdfium"}),
        (".env.full", {"OA_DEPLOY_PROFILE": "full", "OA_KB_PARSER": "docling"}),
        # full-cad：统一配置层枚举限 lite|full → 填 full；CAD 通道为外置服务（注释预留）
        (".env.full-cad", {"OA_DEPLOY_PROFILE": "full", "OA_KB_PARSER": "docling"}),
    ],
)
def test_三档模板档位与解析引擎对齐矩阵(name: str, expected: dict[str, str]) -> None:
    # Act
    keys = _template_active_keys(name)
    # Assert：standards/03 §3 矩阵默认能力集口径（lite=确定性 pdfium；full=docling）
    for key, value in expected.items():
        assert keys.get(key) == value, f"{name}: {key}={keys.get(key)!r}，期望 {value!r}"


def test_full_cad模板_CAD通道注释预留不生效() -> None:
    # Arrange / Act
    raw = (REPO / ".env.full-cad").read_text(encoding="utf-8")
    keys = _template_active_keys(".env.full-cad")
    # Assert：OA_CAD_SERVICE_URL 尚未接线统一配置层——只能以注释出现，绝不作为活跃键
    assert "OA_CAD_SERVICE_URL" in raw, "full-cad 模板应注释预留 CAD 装配键"
    assert "OA_CAD_SERVICE_URL" not in keys


@requires_bash
def test_安装器合成段末次生效(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Arrange：隔离 OA_* 环境变量；合成 lite+local+none 落临时目录的 .env
    # （Settings 的 env_file 相对 CWD 解析——与运行时「bin 壳 cd 到仓库根」同口径）
    for field in Settings.model_fields:
        monkeypatch.delenv(f"OA_{field.upper()}", raising=False)
    monkeypatch.chdir(tmp_path)
    proc = _bash(f'source install.sh && oa_env_synth lite local none > "{(tmp_path / ".env").as_posix()}"')
    assert proc.returncode == 0, proc.stderr

    # Act：Settings 解析合成档（模板默认在前、合成段在后）
    settings = Settings()
    # Assert：dotenv 末次生效——合成段 local 渠道覆盖模板云渠道默认
    assert settings.llm_base_url == "http://127.0.0.1:18001/v1"
    assert settings.llm_model == "local-main"
    assert settings.deploy_profile == "lite"
