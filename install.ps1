# install.ps1 —— ontology-agent 原生安装器（Windows，PowerShell >= 5.1）
# 设计权威：docs/ops/04-多平台安装部署与CLI执行设计.md §3.1（八步）+ §3.3（-SkipApi 混合开发态）
# Linux/macOS 对应 install.sh（语义逐条对齐，验收单同一张，对齐声明见 deploy/README.md）。
# 本文件含中文注释：必须 UTF-8 **带 BOM**（PS 5.1 无 BOM 会按 ANSI 误读中文）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File install.ps1                                   # 交互三问
#   powershell -ExecutionPolicy Bypass -File install.ps1 -SkipApi                          # 混合开发态
#   powershell -ExecutionPolicy Bypass -File install.ps1 -DeployProfile lite -LlmChannel cloud -EmbedChannel ollama
# 八步与幂等语义同 install.sh（①前置自检 ②uv 自举 ③uv sync --frozen ④存储起底 ⑤迁移
# ⑥配置档落位（首配不覆盖：已有 .env 备份 .env.bak-<ts>+diff 提示）⑦oadm doctor 门禁 ⑧摘要）。
# 可 source 形态：全部逻辑在 Oa-* 函数内，点源（. .\install.ps1）只获得函数不执行（测试用）。

[CmdletBinding()]
param(
    [switch]$SkipApi,
    [ValidateSet('lite', 'full', 'full-cad', '')][string]$DeployProfile = '',
    [ValidateSet('local', 'cloud', 'none', '')][string]$LlmChannel = '',
    [ValidateSet('tei', 'ollama', 'none', '')][string]$EmbedChannel = ''
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$MinDiskKB = 2097152   # §3.1 磁盘 >=2G

function Write-OaLog([string]$Message) { Write-Host "[install] $Message" }

function Write-OaDie([string]$Message) {
    Write-Host "[install] X $Message" -ForegroundColor Red
    exit 1
}

# ── 步骤 1：前置自检（python >=3.11 / 磁盘 >=2G / 端口占用；--SkipApi 不查 8000）──

function Test-OaPython {
    foreach ($py in @('python3', 'python')) {
        $cmd = Get-Command $py -ErrorAction SilentlyContinue
        if ($null -ne $cmd) {
            & $cmd.Source -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { return $cmd.Source }
        }
    }
    return $null
}

function Get-OaPythonVersion([string]$PythonExe) {
    (& $PythonExe -c "import sys; print('%d.%d.%d' % sys.version_info[:3])")
}

function Get-OaDiskFreeKB {
    $drive = [System.IO.DriveInfo]::new((Split-Path -Qualifier $Root))
    [math]::Floor($drive.AvailableFreeSpace / 1KB)
}

function Test-OaPortBusy([int]$Port) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(300) -and $client.Connected) { return $true }
        return $false
    } finally { $client.Close() }
}

function Test-OaContainerRunning([string]$Piece) {
    try {
        $status = & docker ps --filter "name=onto-agent-$Piece" --format '{{.Status}}' 2>$null
    } catch { return $false }   # docker 未安装/不可用 → 视为不在场
    if ($LASTEXITCODE -ne 0 -or $null -eq $status) { return $false }
    return @($status -match 'healthy').Count -gt 0
}

function Test-OaPortConflicted([int]$Port, [string]$Piece) {
    (Test-OaPortBusy $Port) -and -not (Test-OaContainerRunning $Piece)
}

function Invoke-OaPreflight {
    Write-OaLog "步骤1 前置自检（python/磁盘/端口）..."
    $pythonExe = Test-OaPython
    if ($null -eq $pythonExe) {
        Write-OaDie "Python >=3.11 未满足：安装 Python 3.11+（python.org / Microsoft Store / uv python install 3.11）后重跑"
    }
    Write-OaLog "  python $(Get-OaPythonVersion $pythonExe) OK"
    $freeKB = Get-OaDiskFreeKB
    if ($freeKB -lt $MinDiskKB) {
        Write-OaDie "磁盘可用不足 2G（当前 ${freeKB}K）：清理空间或将仓库迁移到充足磁盘后重跑"
    }
    Write-OaLog "  磁盘可用 ${freeKB}K OK"
    $pieces = @(@{ Port = 5432; Piece = 'postgres' }, @{ Port = 6379; Piece = 'redis' }, @{ Port = 9000; Piece = 'minio' })
    if (-not $SkipApi) { $pieces = @(@{ Port = 8000; Piece = 'api' }) + $pieces }   # §3.1 端口口径
    foreach ($item in $pieces) {
        if (Test-OaPortConflicted $item.Port $item.Piece) {
            Write-OaDie "端口 $($item.Port) 被外部进程占用（非本平台容器）：停用占用进程，或同步改 deploy/docker-compose*.yml 端口映射与 OA_* 连接配置后重跑"
        }
        if (Test-OaPortBusy $item.Port) {
            Write-OaLog "  端口 $($item.Port) 由本平台 $($item.Piece) 容器占用（healthy）——幂等重装，让行"
        } else {
            Write-OaLog "  端口 $($item.Port) 空闲 OK"
        }
    }
}

# ── 步骤 2：uv 自举（官方脚本，隔离用户目录）──

function Install-OaUv {
    $existing = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        Write-OaLog "步骤2 uv 已在场：$($existing.Source)"
        return
    }
    Write-OaLog "步骤2 未检测到 uv，官方脚本自举..."
    try {
        $script = Invoke-WebRequest -UseBasicParsing 'https://astral.sh/uv/install.ps1'
    } catch {
        Write-OaDie "uv 自举脚本下载失败（$($_.Exception.Message)）：检查网络后重跑，或手动安装 uv"
    }
    Invoke-Expression $script.Content
    $env:Path = "$env:Path;$env:USERPROFILE\.local\bin"
    if ($null -eq (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-OaDie "uv 自举失败：请按 https://docs.astral.sh/uv/getting-started/installation/ 手动安装后重跑"
    }
    Write-OaLog "  uv 自举完成"
}

# ── 步骤 3：依赖装配（uv sync --frozen，lock 忠实）──

function Invoke-OaSyncDeps {
    Write-OaLog "步骤3 依赖装配：uv sync --frozen..."
    Push-Location $Root
    try { & uv sync --frozen } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) {
        Write-OaDie "uv sync 失败：离线/内网环境先配置镜像（`$env:UV_INDEX_URL='https://pypi.tuna.tsinghua.edu.cn/simple'）后重跑"
    }
}

# ── 步骤 4：存储起底（探测既有栈→复用；无则 compose 起底）──

function Test-OaStorageHealthy {
    (Test-OaContainerRunning 'postgres') -and (Test-OaContainerRunning 'redis') -and (Test-OaContainerRunning 'minio')
}

function Invoke-OaEnsureStorage {
    if (Test-OaStorageHealthy) {
        Write-OaLog "步骤4 检测到既有 onto-agent 存储栈（healthy）——复用不重建（幂等，§6 验收 3）"
        return
    }
    Write-OaLog "步骤4 起存储栈（PG(pgvector)/Redis/MinIO：deploy/docker-compose.yml + local overlay）..."
    Push-Location $Root
    try {
        & docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml up -d --remove-orphans
    } catch {
        Pop-Location
        Write-OaDie "docker 不可用（$($_.Exception.Message)）：先安装 Docker Desktop/Engine 后重跑"
    } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) {
        Write-OaDie "存储栈启动失败：核对 docker 可用；国内镜像受限可先 sh deploy/up.sh（华为 SWR 转存）再重跑（两路径幂等）"
    }
    Write-OaLog "  等待存储 healthcheck 转绿（有界 180s）..."
    for ($i = 0; $i -lt 36; $i++) {
        if (Test-OaStorageHealthy) {
            Write-OaLog "  存储栈就绪 OK（PG 5432 / Redis 6379 / MinIO 9000,9001）"
            return
        }
        Start-Sleep -Seconds 5
    }
    Write-OaDie "存储栈未按时转绿：docker ps / docker logs onto-agent-postgres-1 排查后重跑（幂等，不重复造卷）"
}

# ── 步骤 5：迁移（alembic upgrade heads；失败打印 PG 连通诊断）──

function Invoke-OaMigrate {
    Write-OaLog "步骤5 迁移：alembic upgrade heads..."
    Push-Location $Root
    try { & uv run alembic -c services/alembic.ini upgrade heads } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) {
        Write-OaDie "迁移失败：多为 PG 连不通——docker ps 看 onto-agent-postgres-1 是否 healthy；核对 .env 的 OA_PG_*（宿主端口 5432）后重跑（upgrade heads 幂等可重入）"
    }
}

# ── 步骤 6：配置档落位（三问合成 .env；已有则备份+diff 提示，绝不覆盖，§4 首配不覆盖）──

function ConvertTo-OaEnvSynth([string]$ProfileName, [string]$Llm, [string]$Embed) {
    $tmpl = Join-Path $Root ".env.$ProfileName"
    if (-not (Test-Path $tmpl)) { Write-OaDie "模板缺失：$tmpl" }
    $synth = (Get-Content $tmpl -Raw)
    $synth += "`n# ===== 安装器合成段（第 6 步三问结果；追加在文件末尾）=====`n"
    switch ($Llm) {
        'local' { $synth += "OA_LLM_BASE_URL=http://127.0.0.1:18001/v1`nOA_LLM_MODEL=local-main`n" }
        'cloud' { $synth += "OA_LLM_BASE_URL=https://api.deepseek.com`nOA_LLM_MODEL=deepseek-chat`n# 【改我】填入云渠道令牌（密钥形制，绝不入库）`n# OA_LLM_API_KEY=`n" }
        'none' { $synth += "# 模型渠道暂不配置：未设 OA_LLM_BASE_URL=LLM 关闭（后续手编 .env）`n" }
    }
    switch ($Embed) {
        'tei' { $synth += "OA_OLLAMA_BASE_URL=http://127.0.0.1:18002`nOA_EMBED_PROTOCOL=tei`n" }
        'ollama' { $synth += "OA_OLLAMA_BASE_URL=http://localhost:11434`nOA_EMBED_PROTOCOL=ollama`n" }
        'none' { $synth += "# 嵌入暂不配置（不可用自动降级；memory_vector_enabled 默认 false）`n" }
    }
    return $synth
}

function Publish-OaEnv([string]$SynthPath) {
    $envPath = Join-Path $Root '.env'
    if (Test-Path $envPath) {
        $ts = Get-Date -Format 'yyyyMMddHHmmss'
        Copy-Item $envPath "$envPath.bak-$ts"
        Write-OaLog "检测到既有 .env：已备份为 .env.bak-$ts；原档保留不覆盖（ops/04 §4 首配不覆盖）"
        Write-OaLog "合成档与现有档 diff（<= 现有 / => 合成；自行取舍后手编 .env）："
        Compare-Object -ReferenceObject (Get-Content $envPath) -DifferenceObject (Get-Content $SynthPath) |
            ForEach-Object { $mark = if ($_.SideIndicator -eq '<=') { '<' } else { '>' }; "$mark $($_.InputObject)" } |
            Write-Host
        return 'kept'
    }
    Copy-Item $SynthPath $envPath
    Write-OaLog "已写入 $envPath"
    return 'written'
}

# ── 步骤 7：自检门禁（oadm doctor 全绿才过）──

function Invoke-OaDoctor {
    Write-OaLog "步骤7 自检：oadm doctor（八项，ops/04 §3.1 第 7 步同源）..."
    Push-Location $Root
    try { & uv run python -m services.cli.oadm doctor } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) {
        Write-OaDie "oadm doctor 存在红项：按各项修复指引处理后重跑（幂等）"
    }
}

# ── 步骤 8：打印摘要 ──

function Write-OaSummary([string]$ProfileName, [bool]$Skip) {
    Write-Host ""
    Write-Host "================ 安装完成（档位：$ProfileName）================"
    Write-Host "访问地址：http://localhost:8000（api 就绪后；OpenAPI 文档 /docs）"
    Write-Host "种子账号：admin@local（初始占位密码 ChangeMe@FirstLogin——首登后立即改密；值为迁移内公开占位，非凭据）"
    Write-Host "配置档：$ProfileName -> 仓库根 .env（模板=.env.lite/.env.full/.env.full-cad，ops/04 §4）"
    if ($Skip) {
        Write-Host "混合开发态（-SkipApi）：存储与 CLI 已就绪；后端自起：uv run python -m services.main"
    } else {
        Write-Host "起全栈：docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml -f deploy/docker-compose.full.yml --profile full up -d"
    }
    Write-Host "下一步三条命令："
    Write-Host "  bin\oadm.cmd doctor              # 安装自检八项（全绿=安装门禁）"
    Write-Host "  bin\oadm.cmd status              # 全件健康矩阵（--output json 供 CI 消费）"
    Write-Host "  bin\oadm.cmd up --profile lite   # 幂等起栈（full/full-cad 同形，§5）"
    Write-Host "=================================================="
}

# ── 主流程（仅直接执行时运行；点源只获得函数库）──

function Invoke-OaMain {
    if (-not $DeployProfile -or -not $LlmChannel -or -not $EmbedChannel) {
        if ([Environment]::UserInteractive) {
            if (-not $DeployProfile) {
                $resp = Read-Host "配置档位 lite|full|full-cad（默认 lite）"
                $DeployProfile = if ($resp) { $resp } else { 'lite' }
            }
            if (-not $LlmChannel) {
                $resp = Read-Host "LLM 渠道 local(vllm)|cloud(DeepSeek)|none（默认 cloud）"
                $LlmChannel = if ($resp) { $resp } else { 'cloud' }
            }
            if (-not $EmbedChannel) {
                $resp = Read-Host "嵌入渠道 tei|ollama|none（默认 ollama）"
                $EmbedChannel = if ($resp) { $resp } else { 'ollama' }
            }
        } else {
            if (-not $DeployProfile) { $DeployProfile = 'lite' }
            if (-not $LlmChannel) { $LlmChannel = 'none' }
            if (-not $EmbedChannel) { $EmbedChannel = 'none' }
        }
    }
    Write-OaLog "ontology-agent 安装开始（root=$Root，SkipApi=$SkipApi）——docs/ops/04 §3.1 八步"
    Invoke-OaPreflight                                                        # 步骤 1
    Install-OaUv                                                              # 步骤 2
    Invoke-OaSyncDeps                                                         # 步骤 3
    Invoke-OaEnsureStorage                                                    # 步骤 4
    Invoke-OaMigrate                                                          # 步骤 5
    Write-OaLog "步骤6 配置档落位（profile=$DeployProfile llm=$LlmChannel embed=$EmbedChannel）..."   # 步骤 6
    $synth = ConvertTo-OaEnvSynth $DeployProfile $LlmChannel $EmbedChannel
    $tmp = Join-Path $env:TEMP ("oa-env-synth-{0}.tmp" -f [guid]::NewGuid().ToString('N'))
    # 无 BOM UTF-8（python-dotenv 对 BOM 敏感；PS5.1 的 Set-Content utf8 自带 BOM 故不用）
    [System.IO.File]::WriteAllText($tmp, $synth, [System.Text.UTF8Encoding]::new($false))
    $null = Publish-OaEnv $tmp
    Remove-Item $tmp -ErrorAction SilentlyContinue
    Invoke-OaDoctor                                                           # 步骤 7
    Write-OaSummary $DeployProfile ([bool]$SkipApi)                           # 步骤 8
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-OaMain }
