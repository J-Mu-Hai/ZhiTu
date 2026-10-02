<#
.SYNOPSIS
  知途「本地完整体验」——在隔离的 SQLite 上启动后端 + 前端，不碰生产、不部署。

.DESCRIPTION
  在浏览器里体验 P0–阶段 4 的功能，而不需要服务器、Vercel、Nginx 或生产数据库。

  安全边界（这个脚本存在的全部理由）：
  - 默认使用独立的本地库 data/zhitu_local_experience.db，通过**子进程环境变量**
    DATABASE_URL 覆盖，**不改仓库根的 .env**。
  - 若 APP_ENV=production、DATABASE_URL 指向 PostgreSQL、或目标库不是本地 SQLite，
    直接拒绝启动。
  - 不删除已有数据库；要重置必须显式 -Reset，且会先打印完整路径并要求确认。
  - 日志与状态文件都放在 Git 忽略的 logs/ 下；数据库在 Git 忽略的 data/ 下。
  - 从不打印 LLM_API_KEY / DATABASE_URL / APP_SECRET_KEY 或 .env 内容。
  - 只终止「状态文件里记录、且命令行匹配本仓库」的进程；绝不按端口杀未知进程。

  模式（-Mode）：
  - Auto（默认）：根 .env 有真实可用的 LLM_API_KEY → 用真实模型（AGENT_REASONER=auto）；
    没有 → 自动切到脚本回放（AGENT_REASONER=script + 本地 demo fixture）。
  - Real：没有真实 Key 就拒绝启动。
  - Script：固定脚本回放，不消耗任何模型额度。

  注意：本地体验**不验证** HTTPS、Nginx、Vercel、生产 PostgreSQL 或线上 CORS。

.PARAMETER Mode
  Auto | Real | Script，默认 Auto。

.PARAMETER Stop
  停止本脚本记录的进程（先核对命令行/仓库，再停），删除状态文件。

.PARAMETER Status
  显示状态文件里的 PID、端口、模式、健康状态与日志路径。

.PARAMETER DryRun
  只打印将执行的步骤；不建库、不迁移、不起进程、不写状态。

.PARAMETER Reset
  删除本地体验数据库（data/zhitu_local_experience.db）。删除前打印完整路径并要求确认。

.PARAMETER PythonExe
  指定 Python 解释器；默认尝试 conda 环境 zhitu，再尝试常见路径与 PATH。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\dev\dev-up.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\dev\dev-up.ps1 -Mode Script
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\dev\dev-up.ps1 -Status
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\dev\dev-up.ps1 -Stop
#>
[CmdletBinding()]
param(
  [ValidateSet('Auto', 'Real', 'Script')][string]$Mode = 'Auto',
  [switch]$Stop,
  [switch]$Status,
  [switch]$DryRun,
  [switch]$Reset,
  [string]$PythonExe
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------------
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$DotEnvPath = Join-Path $RepoRoot '.env'
$DataDir = Join-Path $RepoRoot 'data'
$DbPath = Join-Path $DataDir 'zhitu_local_experience.db'
$DbUrl = 'sqlite+aiosqlite:///' + ($DbPath -replace '\\', '/')
$LogDir = Join-Path $RepoRoot 'logs\local-experience'
$StateFile = Join-Path $LogDir 'state.json'
$WebDir = Join-Path $RepoRoot 'apps\web'
$WebEnvLocal = Join-Path $WebDir '.env.local'
$Fixture = Join-Path $RepoRoot 'scripts\dev\fixtures\local-demo-script.json'
$ApiUrl = 'http://127.0.0.1:8000'
$WebUrl = 'http://127.0.0.1:5173'
$ApiPort = 8000
$WebPort = 5173

$BackendOut = Join-Path $LogDir 'backend.out.log'
$BackendErr = Join-Path $LogDir 'backend.err.log'
$WebOut = Join-Path $LogDir 'web.out.log'
$WebErr = Join-Path $LogDir 'web.err.log'

function Write-Step([string]$Message) { Write-Host "[dev-up] $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "[dev-up] $Message" -ForegroundColor Green }
function Write-Note([string]$Message) { Write-Host "[dev-up] $Message" -ForegroundColor DarkGray }
function Fail([string]$Message) {
  Write-Host "[dev-up] 拒绝/失败: $Message" -ForegroundColor Red
  exit 1
}

# ---------------------------------------------------------------------------------
# 小工具：读 .env、判断 Key、找 Python、查端口、查进程
# ---------------------------------------------------------------------------------
function Get-EnvFileValue([string]$Path, [string]$Key) {
  if (-not (Test-Path $Path)) { return $null }
  foreach ($line in Get-Content -LiteralPath $Path) {
    $t = $line.Trim()
    if ($t -eq '' -or $t.StartsWith('#')) { continue }
    $i = $t.IndexOf('=')
    if ($i -lt 1) { continue }
    if ($t.Substring(0, $i).Trim() -ne $Key) { continue }
    $v = $t.Substring($i + 1).Trim()
    if ($v.Length -ge 2) {
      if (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'"))) {
        $v = $v.Substring(1, $v.Length - 2)
      }
    }
    return $v
  }
  return $null
}

function Test-LlmKeyReal([string]$Value) {
  if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
  $v = $Value.Trim()
  $lower = $v.ToLowerInvariant()
  $placeholders = @(
    'your-key', 'your_api_key', 'your-api-key', 'yourkey', 'changeme', 'change_me',
    'placeholder', 'todo', 'dummy', 'example', 'test', 'xxx', 'sk-xxx', 'replace_me',
    '<key>', '<your-key>', 'none', 'null'
  )
  if ($placeholders -contains $lower) { return $false }
  if ($v -match '^[<\[]') { return $false }
  if ($v -match '(?i)(your[_-]?key|placeholder|example|changeme|replace[_-]?me|dummy)') { return $false }
  if ($v.Length -lt 16) { return $false }
  return $true
}

function Resolve-PythonExe([string]$Explicit) {
  $candidates = New-Object System.Collections.Generic.List[string]
  if ($Explicit) { $candidates.Add($Explicit) }
  if ($env:ZHITU_PYTHON) { $candidates.Add($env:ZHITU_PYTHON) }

  $conda = $null
  $cmd = Get-Command conda -ErrorAction SilentlyContinue
  if ($cmd) { $conda = $cmd.Source }
  if (-not $conda -and $env:CONDA_EXE) { $conda = $env:CONDA_EXE }
  if ($conda) {
    try {
      $base = (& $conda info --base 2>$null | Select-Object -First 1)
      if ($base) { $candidates.Add((Join-Path $base.Trim() 'envs\zhitu\python.exe')) }
    } catch { }
  }
  $common = @(
    (Join-Path $env:USERPROFILE 'miniconda3\envs\zhitu\python.exe'),
    (Join-Path $env:USERPROFILE 'anaconda3\envs\zhitu\python.exe'),
    (Join-Path $env:USERPROFILE 'AppData\Local\miniconda3\envs\zhitu\python.exe'),
    'C:\ProgramData\miniconda3\envs\zhitu\python.exe',
    'C:\ProgramData\anaconda3\envs\zhitu\python.exe'
  )
  foreach ($p in $common) { if ($p) { $candidates.Add($p) } }

  foreach ($c in $candidates) {
    if ($c -and (Test-Path -LiteralPath $c)) { return (Resolve-Path -LiteralPath $c).Path }
  }
  foreach ($name in @('python.exe', 'python')) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
  }
  throw "找不到 Python。请用 -PythonExe 指定(推荐本机 conda 环境 zhitu 的 python.exe)。"
}

function Get-ProcInfo([int]$Id) {
  try { return Get-CimInstance Win32_Process -Filter "ProcessId = $Id" -ErrorAction Stop }
  catch { return $null }
}

function Get-PortOwnerPid([int]$Port) {
  try {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
  } catch { }
  try {
    $match = netstat -ano | Select-String -Pattern ":$Port\s" | Select-String -Pattern 'LISTENING' | Select-Object -First 1
    if ($match) {
      $parts = ($match.Line -split '\s+') | Where-Object { $_ -ne '' }
      if ($parts.Count -ge 5) { return [int]$parts[-1] }
    }
  } catch { }
  return $null
}

function Get-DescendantIds([int]$RootId) {
  $result = New-Object System.Collections.Generic.List[int]
  $all = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId
  $queue = New-Object System.Collections.Generic.Queue[int]
  $queue.Enqueue($RootId)
  while ($queue.Count -gt 0) {
    $cur = $queue.Dequeue()
    foreach ($p in $all) {
      if ([int]$p.ParentProcessId -eq $cur) {
        $child = [int]$p.ProcessId
        if (-not $result.Contains($child)) {
          $result.Add($child)
          $queue.Enqueue($child)
        }
      }
    }
  }
  return $result
}

function Test-CommandLineMatches([object]$Info, [string[]]$Keywords) {
  if (-not $Info) { return $false }
  $cl = [string]$Info.CommandLine
  if ([string]::IsNullOrWhiteSpace($cl)) { return $false }
  foreach ($kw in $Keywords) {
    if ($cl.IndexOf($kw, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
  }
  return $false
}

function Stop-VerifiedPid([int]$Id, [string[]]$Keywords) {
  $info = Get-ProcInfo $Id
  if (-not $info) { return $false }
  if (-not (Test-CommandLineMatches $info $Keywords)) {
    Write-Host "[dev-up] 跳过 PID ${Id}(命令行不像本脚本启动的进程,不终止)。" -ForegroundColor Yellow
    return $false
  }
  $ids = @($Id) + @(Get-DescendantIds $Id)
  foreach ($x in ($ids | Select-Object -Unique)) {
    Stop-Process -Id $x -Force -ErrorAction SilentlyContinue
  }
  return $true
}

function Read-State() {
  if (-not (Test-Path -LiteralPath $StateFile)) { return $null }
  try { return (Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json) } catch { return $null }
}
function Get-Prop([object]$Obj, [string]$Name) {
  if ($null -eq $Obj) { return $null }
  $p = $Obj.PSObject.Properties[$Name]
  if ($null -eq $p) { return $null }
  return $p.Value
}

# ---------------------------------------------------------------------------------
# 安全 / 模式 / 端口
# ---------------------------------------------------------------------------------
function Assert-LocalSafety() {
  if ($env:APP_ENV -and $env:APP_ENV.ToLowerInvariant() -eq 'production') {
    Fail "进程环境里 APP_ENV=production;本地体验拒绝使用生产配置。"
  }
  $fileAppEnv = Get-EnvFileValue $DotEnvPath 'APP_ENV'
  if ($fileAppEnv -and $fileAppEnv.ToLowerInvariant() -eq 'production') {
    Fail "根 .env 里 APP_ENV=production;本脚本只跑本地开发,拒绝启动。"
  }
  foreach ($src in @($env:DATABASE_URL, (Get-EnvFileValue $DotEnvPath 'DATABASE_URL'))) {
    if ($src -and -not ($src.TrimStart().StartsWith('sqlite'))) {
      Fail "检测到非 SQLite 的 DATABASE_URL(值已隐藏);本地体验只允许独立 SQLite,拒绝启动。"
    }
  }
  if ($DbPath -notlike (Join-Path $RepoRoot 'data\*')) {
    Fail "目标数据库不在仓库 data\ 下:$DbPath"
  }
  if (-not $DbPath.EndsWith('.db')) {
    Fail "目标数据库看起来不是本地 SQLite 文件:$DbPath"
  }
}

function Resolve-EffectiveMode() {
  $key = Get-EnvFileValue $DotEnvPath 'LLM_API_KEY'
  if (-not $key) { $key = $env:LLM_API_KEY }
  $hasRealKey = Test-LlmKeyReal $key
  $effective = $Mode
  if ($Mode -eq 'Auto') {
    if ($hasRealKey) { $effective = 'Real' } else { $effective = 'Script' }
  }
  if ($Mode -eq 'Real' -and -not $hasRealKey) {
    Fail "Mode=Real 需要根 .env 里有真实 LLM_API_KEY;现在没有或它看起来是占位符。可改用 -Mode Script 做脚本演示。"
  }
  $reasoner = if ($effective -eq 'Real') { 'auto' } else { 'script' }
  return [pscustomobject]@{ Effective = $effective; Reasoner = $reasoner; HasRealKey = $hasRealKey }
}

function Assert-NoOursRunning($State) {
  if ($null -eq $State) { return }
  foreach ($name in @('backendPid', 'backendListenPid', 'webPid', 'webListenPid')) {
    $v = Get-Prop $State $name
    if ($v -and (Get-Process -Id ([int]$v) -ErrorAction SilentlyContinue)) {
      Fail "检测到本脚本上次启动的进程仍在运行(PID $v)。先运行 -Stop。"
    }
  }
}

function Assert-PortsFreeForStart($State) {
  $recorded = @()
  foreach ($name in @('backendPid', 'backendListenPid', 'webPid', 'webListenPid')) {
    $v = Get-Prop $State $name
    if ($v) { $recorded += [int]$v }
  }
  foreach ($port in @($ApiPort, $WebPort)) {
    $owner = Get-PortOwnerPid $port
    if (-not $owner) { continue }
    if ($recorded -contains $owner) {
      Fail "端口 $port 被本脚本上次启动的进程(PID $owner)占用;先运行 -Stop。"
    }
    $info = Get-ProcInfo $owner
    $name = if ($info) { [string]$info.Name } else { '未知进程' }
    Fail "端口 $port 已被 PID $owner($name)占用,且不是本脚本启动的。请先自行处理;脚本不会终止不属于自己的进程。"
  }
}

function Ensure-WebEnvLocal() {
  if (Test-Path -LiteralPath $WebEnvLocal) {
    Write-Note "apps/web/.env.local 已存在,保持原样(不覆盖)。"
    return
  }
  Set-Content -LiteralPath $WebEnvLocal -Value "NEXT_PUBLIC_API_BASE_URL=$ApiUrl`n" -Encoding UTF8
  Write-Note "已创建 apps/web/.env.local(只在不存在时创建)。"
}

function Wait-Http([string]$Url, [int]$TimeoutSec) {
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    try {
      $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
      if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 400) { return $true }
    } catch { }
    Start-Sleep -Milliseconds 1500
  }
  return $false
}

# ---------------------------------------------------------------------------------
# 状态 / 停止 / 状态显示
# ---------------------------------------------------------------------------------
function Show-Status() {
  $state = Read-State
  if ($null -eq $state) {
    Write-Note "没有本地体验在运行(找不到状态文件:$StateFile)。"
    return
  }
  Write-Host ''
  Write-Host '知途 · 本地体验状态' -ForegroundColor Green
  Write-Host ("  模式      {0}(AGENT_REASONER={1})" -f (Get-Prop $state 'mode'), (Get-Prop $state 'reasoner'))
  Write-Host ("  数据库    {0}" -f (Get-Prop $state 'dbPath'))
  Write-Host ("  后端      {0}/ready" -f (Get-Prop $state 'apiUrl'))
  Write-Host ("  前端      {0}/workbench" -f (Get-Prop $state 'webUrl'))
  Write-Host ("  启动时间  {0}" -f (Get-Prop $state 'startedAt'))
  Write-Host '  PID:'
  foreach ($name in @('backendPid', 'backendListenPid', 'webPid', 'webListenPid')) {
    $v = Get-Prop $state $name
    if (-not $v) { continue }
    $info = Get-ProcInfo ([int]$v)
    $alive = if ($info) { '运行中' } else { '已退出' }
    $pname = if ($info) { [string]$info.Name } else { '-' }
    Write-Host ("    {0,-18} {1}  {2}  {3}" -f $name, $v, $alive, $pname)
  }
  Write-Host '  健康:'
  $apiReady = Wait-Http "$ApiUrl/ready" 3
  $webReady = Wait-Http "$WebUrl/workbench" 3
  Write-Host ("    /ready      {0}" -f $(if ($apiReady) { 'OK' } else { '未就绪' }))
  Write-Host ("    /workbench  {0}" -f $(if ($webReady) { 'OK' } else { '未就绪' }))
  Write-Host ("  日志目录  {0}" -f $LogDir)
  Write-Host ''
}

function Invoke-Stop() {
  $state = Read-State
  if ($null -eq $state) {
    Write-Note "没有状态文件,没有本脚本记录的进程可停。"
    return
  }
  Write-Step '停止本脚本记录的进程(先核对命令行)…'
  $targets = @(
    @{ Pid = (Get-Prop $state 'backendPid');       Kw = @('uvicorn', 'backend.api.main') },
    @{ Pid = (Get-Prop $state 'backendListenPid'); Kw = @('uvicorn', 'backend.api.main') },
    @{ Pid = (Get-Prop $state 'webPid');           Kw = @('npm-cli.js', 'npm.cmd', 'npm') },
    @{ Pid = (Get-Prop $state 'webListenPid');     Kw = @('node_modules\next', 'next\dist\bin\next') }
  )
  $stopped = 0
  foreach ($t in $targets) {
    if (-not $t.Pid) { continue }
    $ownerId = 0
    if (-not [int]::TryParse(([string]$t.Pid), [ref]$ownerId)) { continue }
    if ($ownerId -le 0) { continue }
    if (Stop-VerifiedPid $ownerId $t.Kw) { $stopped++ }
  }
  Remove-Item -LiteralPath $StateFile -Force -ErrorAction SilentlyContinue
  # 等端口释放
  foreach ($port in @($ApiPort, $WebPort)) {
    for ($i = 0; $i -lt 20; $i++) {
      if (-not (Get-PortOwnerPid $port)) { break }
      Start-Sleep -Milliseconds 300
    }
  }
  Write-Ok "已停止(处理了 $stopped 个根进程及其子进程),状态文件已删除。"
}

# ---------------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------------
if ($Stop) { Invoke-Stop; exit 0 }
if ($Status) { Show-Status; exit 0 }

Assert-LocalSafety
$resolved = Resolve-EffectiveMode
$python = Resolve-PythonExe $PythonExe

if ($DryRun) {
  $existingState = Read-State
  $apiOwner = Get-PortOwnerPid $ApiPort
  $webOwner = Get-PortOwnerPid $WebPort
  Write-Host ''
  Write-Host '[dev-up] DryRun:只打印拟执行步骤,不建库、不迁移、不起进程、不写状态。' -ForegroundColor Cyan
  Write-Host ("  Python      {0}" -f $python)
  Write-Host ("  模式        {0}(AGENT_REASONER={1})" -f $resolved.Effective, $resolved.Reasoner)
  Write-Host ("  真实 Key    {0}" -f $(if ($resolved.HasRealKey) { '检测到(已隐藏)' } else { '未检测到' }))
  Write-Host ("  本地数据库  {0}" -f $DbPath)
  Write-Host ("  数据库 URL  {0}" -f $DbUrl)
  Write-Host ("  状态文件    {0}" -f $StateFile)
  Write-Host ("  日志目录    {0}" -f $LogDir)
  Write-Host ("  端口 8000   {0}" -f $(if ($apiOwner) { "被 PID $apiOwner 占用" } else { '空闲' }))
  Write-Host ("  端口 5173   {0}" -f $(if ($webOwner) { "被 PID $webOwner 占用" } else { '空闲' }))
  Write-Host '  将依次执行:'
  Write-Host ("    1. 设置子进程 DATABASE_URL={0}" -f $DbUrl)
  Write-Host '    2. <python> -m alembic -c backend/alembic.ini upgrade head'
  Write-Host '    3. 后台启动 uvicorn backend.api.main:app --port 8000'
  if ($resolved.Effective -eq 'Script') {
    Write-Host ("       子进程 AGENT_REASONER=script, ZHITU_SCRIPTED_ACTIONS={0}" -f $Fixture)
  } else {
    Write-Host '       子进程 AGENT_REASONER=auto(使用根 .env 的真实 Key,值不打印)'
  }
  Write-Host ("    4. 在 apps/web 后台启动 npm run dev(NEXT_PUBLIC_API_BASE_URL={0})" -f $ApiUrl)
  Write-Host ("    5. 等待 {0}/ready 与 {1}/workbench" -f $ApiUrl, $WebUrl)
  Write-Host '    6. 写状态文件并打印地址 / PID / 日志路径'
  if ($existingState) { Write-Note '（检测到已有状态文件;正式启动前会要求先 -Stop,或由 -Reset 处理数据库。）' }
  Write-Host ''
  exit 0
}

# ---- 正式启动 ----
Assert-NoOursRunning (Read-State)
Assert-PortsFreeForStart (Read-State)

if ($Reset -and (Test-Path -LiteralPath $DbPath)) {
  Write-Host '[dev-up] -Reset:将删除下面这个本地体验数据库(只删这一个文件,不碰生产):' -ForegroundColor Yellow
  Write-Host ("  {0}" -f $DbPath)
  $answer = Read-Host '确认删除请输入 yes'
  if ($answer -ne 'yes') { Fail '已取消,没有删除任何东西。' }
  Remove-Item -LiteralPath $DbPath -Force
  Write-Ok '已删除本地体验数据库。'
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

# 子进程环境：只在**本进程**里设，End 时随进程消失；不改 .env。
$env:DATABASE_URL = $DbUrl
$env:APP_ENV = 'development'
$env:CORS_ORIGINS = 'http://127.0.0.1:5173,http://localhost:5173'
$env:AGENT_REASONER = $resolved.Reasoner
if ($resolved.Effective -eq 'Script') {
  $env:ZHITU_SCRIPTED_ACTIONS = $Fixture
} else {
  Remove-Item Env:\ZHITU_SCRIPTED_ACTIONS -ErrorAction SilentlyContinue
}
if (-not (Get-EnvFileValue $DotEnvPath 'APP_SECRET_KEY') -and -not $env:APP_SECRET_KEY) {
  $env:APP_SECRET_KEY = 'local-experience-not-a-secret'
}
$env:NEXT_PUBLIC_API_BASE_URL = $ApiUrl

Ensure-WebEnvLocal

Write-Step '在隔离库上执行迁移(不改 .env)…'
Push-Location $RepoRoot
try {
  & $python -m alembic -c backend/alembic.ini upgrade head
  if ($LASTEXITCODE -ne 0) { Fail "alembic upgrade head 失败(退出码 $LASTEXITCODE)。" }
} finally {
  Pop-Location
}
Write-Ok "迁移完成:$DbPath"

$backend = $null
$web = $null
try {
  Write-Step '后台启动后端(窗口隐藏,日志进 logs/local-experience)…'
  $backend = Start-Process -FilePath $python `
    -ArgumentList @('-m', 'uvicorn', 'backend.api.main:app', '--port', "$ApiPort") `
    -WorkingDirectory $RepoRoot -WindowStyle Hidden `
    -RedirectStandardOutput $BackendOut -RedirectStandardError $BackendErr -PassThru

  $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
  if (-not $npm) { $npm = Get-Command npm -ErrorAction SilentlyContinue }
  if (-not $npm) { Fail '找不到 npm;请确认 Node 已安装且在 PATH 上。' }

  Write-Step '后台启动前端(npm run dev)…'
  $web = Start-Process -FilePath $npm.Source -ArgumentList @('run', 'dev') `
    -WorkingDirectory $WebDir -WindowStyle Hidden `
    -RedirectStandardOutput $WebOut -RedirectStandardError $WebErr -PassThru

  Write-Step "等待后端 $ApiUrl/ready …"
  if (-not (Wait-Http "$ApiUrl/ready" 90)) {
    Write-Host '[dev-up] 后端没有就绪,后端日志尾部:' -ForegroundColor Red
    if (Test-Path $BackendErr) { Get-Content -LiteralPath $BackendErr -Tail 40 }
    if (Test-Path $BackendOut) { Get-Content -LiteralPath $BackendOut -Tail 20 }
    throw '后端未通过 /ready'
  }
  Write-Ok '后端已就绪。'

  Write-Step "等待前端 $WebUrl/workbench …(首次编译可能要一会儿)"
  if (-not (Wait-Http "$WebUrl/workbench" 180)) {
    Write-Host '[dev-up] 前端没有就绪,前端日志尾部:' -ForegroundColor Red
    if (Test-Path $WebErr) { Get-Content -LiteralPath $WebErr -Tail 40 }
    if (Test-Path $WebOut) { Get-Content -LiteralPath $WebOut -Tail 20 }
    throw '前端未通过 /workbench'
  }
  Write-Ok '前端已就绪。'

  $backendListen = Get-PortOwnerPid $ApiPort
  $webListen = Get-PortOwnerPid $WebPort

  $state = [ordered]@{
    startedAt        = (Get-Date).ToString('o')
    mode             = $resolved.Effective
    reasoner         = $resolved.Reasoner
    python           = $python
    repoDir          = $RepoRoot
    dbPath           = $DbPath
    dbUrl            = $DbUrl
    apiUrl           = $ApiUrl
    webUrl           = $WebUrl
    backendPid       = $backend.Id
    backendListenPid = $backendListen
    webPid           = $web.Id
    webListenPid     = $webListen
    backendOutLog    = $BackendOut
    backendErrLog    = $BackendErr
    webOutLog        = $WebOut
    webErrLog        = $WebErr
  }
  ($state | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $StateFile -Encoding UTF8

  Write-Host ''
  Write-Ok '本地完整体验已启动(没有碰生产数据库、没有部署任何东西)。'
  Write-Host ("  浏览器地址  {0}/workbench" -f $WebUrl) -ForegroundColor White
  Write-Host ("  模式        {0}{1}" -f $resolved.Effective, $(if ($resolved.Effective -eq 'Script') { '(脚本回放,不消耗模型额度)' } else { '(真实模型)' }))
  Write-Host ("  后端 PID    {0}(监听 PID {1})" -f $backend.Id, $backendListen)
  Write-Host ("  前端 PID    {0}(监听 PID {1})" -f $web.Id, $webListen)
  Write-Host ("  数据库      {0}" -f $DbPath)
  Write-Host ("  日志        {0}" -f $LogDir)
  Write-Host ("  状态        powershell -ExecutionPolicy Bypass -File scripts\dev\dev-up.ps1 -Status")
  Write-Host ("  停止        powershell -ExecutionPolicy Bypass -File scripts\dev\dev-up.ps1 -Stop")
  if ($resolved.Effective -eq 'Script') {
    Write-Host ''
    Write-Note '脚本回放说明:界面来源徽标会写「脚本回放」。请按 docs/07-DEVELOPMENT.md 的体验顺序对话。'
  }
  Write-Host ''
}
catch {
  Write-Host ("[dev-up] 启动失败:{0}" -f $_.Exception.Message) -ForegroundColor Red
  foreach ($p in @($web, $backend)) {
    if ($p) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
  }
  exit 1
}
