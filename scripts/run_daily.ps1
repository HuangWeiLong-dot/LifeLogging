# LifeLogging daily pipeline
#
# 用法：
#   powershell -File scripts\run_daily.ps1                 # 自动（推荐，计划任务用这个）
#   powershell -File scripts\run_daily.ps1 -Date 2026-10-12  # 手动补指定某天
#   powershell -File scripts\run_daily.ps1 -DryRun           # 只生成不写文件
#
# ── 为什么不能简单用 "today" ────────────────────────────────────────
# 计划任务设在 23:00，且开了「错过的任务开机后补跑」。
# 如果日期写成 "today"：22:00 关机、次日 09:00 开机 → 补跑时
# "today" 已经是新的一天了，于是它去处理刚开机这一天（几乎没有数据），
# 而你真正丢失的那一天**永远不会被处理**。原始数据其实还在，
# 但没有任何东西会去回补它。
#
# 所以改成水位线逻辑：
#   · state/last_processed.json 记「最后成功生成日记的那一天」
#   · 每次运行处理 从水位线+1 到目标日 之间的每一天
#   · 目标日 = 今天（仅当已过 NightlyHour）否则昨天
#     —— 否则早上补跑会把「只用了半天数据的一天」标成已完成
#
# ── 任何一步失败都不中断后面的步骤 ──────────────────────────────────
# 采不到位置数据不该导致日记写不出来 —— 那只会让你第二天更不想用它。

param(
    [string]$Date = "",
    [int]$NightlyHour = 23,
    [int]$MaxBackfillDays = 14,
    [switch]$SkipAI,
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# 子进程输出走 UTF-8 —— 两边都要设。
#
# 光设 PYTHONIOENCODING 不够：那只管 Python **写**什么，
# 而 PowerShell **读**管道用的编码由 [Console]::OutputEncoding 决定，
# 默认是 OEM 代码页（中文系统是 936/GBK）。
# 一边写 UTF-8、一边按 GBK 读，结果是 '·' 变成 '路'（0xC2B7 在 GBK 里正是「路」），
# 日志里所有 → ✓ ⚠️ 都会变成乱码。
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$LogDir = Join-Path $Root "state\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$LogFile = Join-Path $LogDir "run_$Stamp.log"

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

$StateFile = Join-Path $Root "state\last_processed.json"

function Write-Log($msg) {
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Invoke-Step($name, $script, $scriptArgs) {
    Write-Log "   -- $name"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $out = & $Py $script @scriptArgs 2>&1
    $code = $LASTEXITCODE
    $sw.Stop()

    foreach ($line in $out) {
        Write-Host "      $line"
        Add-Content -Path $LogFile -Value "      $line" -Encoding UTF8
    }

    if ($code -eq 0) {
        Write-Log "   OK   $name  ($($sw.Elapsed.TotalSeconds.ToString('0.0'))s)"
        return $true
    }
    Write-Log "   WARN $name exit=$code -- continuing to the next step"
    return $false
}

function Get-LastProcessed {
    if (-not (Test-Path $StateFile)) { return $null }
    try {
        $j = Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($j.last_date) { return ([datetime]::Parse($j.last_date)).Date }
    } catch {
        Write-Log "   WARN watermark file unreadable; treating as never-run"
    }
    return $null
}

function Set-LastProcessed($d) {
    $obj = [ordered]@{
        last_date  = $d.ToString("yyyy-MM-dd")
        updated_at = (Get-Date).ToString("s")
    }
    $obj | ConvertTo-Json | Set-Content -Path $StateFile -Encoding UTF8
}

function Invoke-OneDay($d) {
    $ds = $d.ToString("yyyy-MM-dd")
    Write-Log "=== $ds ==="

    # 0. 预算闸门 —— 超了就直接停止，不要按天重复撞墙
    $gateOut = & $Py "scripts\cost_guard.py" gate 2>&1
    $gateCode = $LASTEXITCODE
    foreach ($line in $gateOut) {
        Write-Host "      $line"
        Add-Content -Path $LogFile -Value "      $line" -Encoding UTF8
    }
    if ($gateCode -ne 0) {
        Write-Log "   BLOCKED budget pre-flight failed; stopping this run."
        return $false
    }

    # 1. 采集
    Invoke-Step "collect-desktop" "scripts\collect_desktop.py" @("--date", $ds) | Out-Null
    Invoke-Step "collect-phone"   "scripts\collect_phone.py"   @("--date", $ds) | Out-Null
    Invoke-Step "gadgetbridge"    "middleware\ingest_gadgetbridge.py" @("--date", $ds) | Out-Null

    # 2. 归一化
    $normalized = Invoke-Step "normalize" "scripts\normalize.py" @("--date", $ds)

    # 3. 生成日记
    if ($SkipAI) {
        Write-Log "   SKIP AI（-SkipAI）"
        return $true
    }
    if (-not $normalized) {
        Write-Log "   SKIP journal -- normalize failed, no journal generated"
        return $false
    }

    $genArgs = @("--date", $ds)
    if ($DryRun) { $genArgs += "--dry-run" }
    return (Invoke-Step "journal" "scripts\generate_journal.py" $genArgs)
}

# ── 补齐环境变量 ────────────────────────────────────────────────────
# setx 写的是 HKCU\Environment。计划任务会继承到，但**从旧 shell 启动的
# 进程不会** —— 于是出现「手动跑失败、任务里却能跑」或反过来的迷惑现象。
# 这里显式兜底一次，消除这一整类问题。
# 只从注册表读进进程环境，不回写、不落盘到项目目录。
foreach ($name in @("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY")) {
    $processVal = [Environment]::GetEnvironmentVariable($name, "Process")
    if (-not $processVal) {
        $userVal = [Environment]::GetEnvironmentVariable($name, "User")
        if ($userVal) {
            [Environment]::SetEnvironmentVariable($name, $userVal, "Process")
            Write-Log "Backfilled $name from the user environment"
        }
    }
}

$hasKey = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "Process")
if (-not $hasKey) {
    Write-Log "WARN DEEPSEEK_API_KEY not found -- journal generation will fail."
    Write-Log "     Set it: setx DEEPSEEK_API_KEY \"sk-...\"  then reopen the terminal"
}

# ── 决定处理哪几天 ──────────────────────────────────────────────────

$today = (Get-Date).Date
$targets = @()
$manual = $false

if ($Date -ne "") {
    $manual = $true
    if ($Date -eq "today")          { $targets = @($today) }
    elseif ($Date -eq "yesterday")  { $targets = @($today.AddDays(-1)) }
    else                            { $targets = @(([datetime]::Parse($Date)).Date) }
    Write-Log "Manual run for $($targets[0].ToString('yyyy-MM-dd')) (watermark not updated)"
}
else {
    $last = Get-LastProcessed
    # 今天本身只在「到点之后」才处理。否则早上补跑会把只用了半天数据的
    # 一天标成已完成，那一整天的数据就再也不补了。
    $includeToday = (Get-Date).Hour -ge $NightlyHour
    if ($includeToday) { $toDate = $today } else { $toDate = $today.AddDays(-1) }

    if ($last) { $fromDate = $last.AddDays(1) } else { $fromDate = $toDate }

    if ($fromDate -gt $toDate) {
        Write-Log "Nothing to do (watermark $($last), target $($toDate.ToString('yyyy-MM-dd')))"
        exit 0
    }

    $span = ($toDate - $fromDate).Days + 1
    if ($span -gt $MaxBackfillDays) {
        $fromDate = $toDate.AddDays(-($MaxBackfillDays - 1))
        Write-Log "WARN $span days pending, over the $MaxBackfillDays cap; backfilling the most recent only"
    }

    $cursor = $fromDate
    while ($cursor -le $toDate) {
        $targets += $cursor
        $cursor = $cursor.AddDays(1)
    }
    Write-Log "Auto mode: $($targets.Count) day(s) to process $($fromDate.ToString('yyyy-MM-dd')) -> $($toDate.ToString('yyyy-MM-dd'))"
}

Write-Log ("=" * 60)

$okDays = 0
$failDays = @()

foreach ($d in $targets) {
    if (Invoke-OneDay $d) {
        $okDays++
        if (-not $manual) { Set-LastProcessed $d }
    } else {
        $failDays += $d.ToString("yyyy-MM-dd")
    }
}

# ── 每周日核对成本 ──────────────────────────────────────────────────
if ((Get-Date).DayOfWeek -eq "Sunday") {
    Invoke-Step "cost-reconcile" "scripts\reconcile_cost.py" @("--days", "7", "--write-vault") | Out-Null
}

Write-Log ("=" * 60)
$failNote = ""
if ($failDays.Count -gt 0) { $failNote = ", failed: $($failDays -join ', ')" }
Write-Log "Done -- $okDays day(s) succeeded$failNote"
Write-Log "Log: $LogFile"

# 清理 30 天前的日志
Get-ChildItem $LogDir -Filter "run_*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue
