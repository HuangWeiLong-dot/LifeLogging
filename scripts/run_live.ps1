# LifeLogging live publisher
#
# 站点 LIFE 页的实时数据源。和 run_daily.ps1 的分工：
#   run_daily.ps1   按天处理：采集全部源 → 归一化 → 日记 / 问答 / 周回顾 →
#                   水位线 → 每晚 23:00 发布近 30 天（正式版）。
#   run_live.ps1    不推进水位线、不调 AI 生成日记，只做三件事：
#                   1) 把今天到目前为止的电脑 / 手机数据拉进本地 events.db
#                   2) normalize 今天 → 回写 blocks
#                   3) 把今天的聚合 upsert 到 /api/life，并整份覆写 /api/life/now
#                      （now 含位置/坐标、实时问答、电脑/手机/手表；中文先翻成英文）
#
# 由计划任务 LifeLogging-Live 每 2 分钟拉起（见 setup_live_task.ps1）。
# 重采集（Dawarich 位置、Gadgetbridge 手表、AI 平台账单）默认每 15 分钟一次，
# 避免每 2 分钟都去下整份手表库 / 撞平台接口。
#
# 原始归档（raw）整体默认**不在这里发**：整份 publish_life_raw.py 会调 LLM 翻译，
# 高频跑既费钱又会把当前坐标、日记、问答实时公开。需要的话用 -RawMinutes 30 显式打开。
# 唯一例外是 **blocks**：站点七维详情按天拉 /api/life/raw/blocks/<day>，只有这个脚本
# 会发；不补的话当天的应用 / 窗口归因要等 23:00 才出现（白天点开就是不可用）。
# 所以重采集那一档顺带发一份当天的 blocks（--kinds blocks）：只发这一种文档，
# 翻译命中的是已有缓存，只有新出现的窗口标题会调用模型（通常一天几次）。
#
# 用法：
#   powershell -File scripts\run_live.ps1                    # 正常一轮
#   powershell -File scripts\run_live.ps1 -NoPublish         # 只采集
#   powershell -File scripts\run_live.ps1 -DryRun            # 采集 + dry-run 发布
#   powershell -File scripts\run_live.ps1 -RawMinutes 30     # 每 30 分钟也发 raw

param(
    [int]$HeavyMinutes = 15,
    [int]$RawMinutes = 0,
    [switch]$NoPublish,
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# 子进程输出统一 UTF-8（和 run_daily.ps1 同样的原因）
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$LogDir = Join-Path $Root "state\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("live_" + (Get-Date -Format "yyyyMMdd") + ".log")
$StateFile = Join-Path $Root "state\live_state.json"
$LockFile = Join-Path $Root "state\live.lock"

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

function Write-Log($msg) {
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Invoke-Py($name, $script, [string[]]$scriptArgs, [switch]$ShowOutput) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $out = & $Py $script @scriptArgs 2>&1
    $code = $LASTEXITCODE
    $sw.Stop()

    if ($code -eq 0) {
        Write-Log "OK   $name ($($sw.Elapsed.TotalSeconds.ToString('0.0'))s)"
        if ($ShowOutput) {
            foreach ($line in $out) { Add-Content -Path $LogFile -Value "      $line" -Encoding UTF8 }
        }
        return $true
    }

    Write-Log "WARN $name exit=$code ($($sw.Elapsed.TotalSeconds.ToString('0.0'))s) -- continuing"
    $tail = @($out | Select-Object -Last 12)
    foreach ($line in $tail) { Add-Content -Path $LogFile -Value "      $line" -Encoding UTF8 }
    return $false
}

function Read-State {
    if (-not (Test-Path $StateFile)) {
        return [pscustomobject]@{ last_heavy = $null; last_raw = $null }
    }
    try {
        $j = Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        return [pscustomobject]@{
            last_heavy = $j.last_heavy
            last_raw   = $j.last_raw
        }
    } catch {
        return [pscustomobject]@{ last_heavy = $null; last_raw = $null }
    }
}

function Write-State($state) {
    $state | ConvertTo-Json | Set-Content -Path $StateFile -Encoding UTF8
}

function Is-Due($iso, $minutes) {
    if ($minutes -le 0) { return $false }
    if (-not $iso) { return $true }
    try {
        return ((Get-Date) - [datetime]::Parse($iso)).TotalMinutes -ge $minutes
    } catch {
        return $true
    }
}

# 采集脚本都往本地 events API 写；它没起来的话整轮都会是空数据。
# 和 run_daily.ps1 的 Ensure-EventsApi 同一个思路，只是这里不打印整段错误。
function Ensure-EventsApi {
    $apiBase = "http://127.0.0.1:18787"
    try {
        $parsed = & $Py -c "import sys;sys.path.insert(0,'scripts');from common import load_config;print(load_config()['sources']['events_api']['api_base'])"
        if ($parsed) { $apiBase = "$parsed".Trim() }
    } catch { }

    try {
        $r = Invoke-WebRequest -Uri "$apiBase/health" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -eq 200) { return $true }
    } catch { }

    try {
        $u = [uri]$apiBase
    } catch {
        Write-Log "WARN events API base is not a URL: $apiBase"
        return $false
    }
    $hostName = $u.Host
    if (-not $hostName) { $hostName = "127.0.0.1" }
    $port = $u.Port
    if (-not $port -or $port -lt 1) { $port = 18787 }

    Write-Log "events API is down -- starting uvicorn on ${hostName}:${port}"
    Start-Process -FilePath $Py `
        -ArgumentList @("-m", "uvicorn", "middleware.events_api:app",
                        "--host", "$hostName", "--port", "$port", "--log-level", "warning") `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "events_api.out.log") `
        -RedirectStandardError (Join-Path $LogDir "events_api.err.log")

    for ($i = 0; $i -lt 24; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $r = Invoke-WebRequest -Uri "$apiBase/health" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            if ($r.StatusCode -eq 200) {
                Write-Log "events API is up"
                return $true
            }
        } catch { }
    }
    Write-Log "WARN events API did not come up; collectors may write nothing this round"
    return $false
}

# ── 单实例 ────────────────────────────────────────────────────────────
# 计划任务已经设了 IgnoreNew；这里再挡一次手动运行和任务撞车。
if (Test-Path $LockFile) {
    $ageMin = ((Get-Date) - (Get-Item $LockFile).LastWriteTime).TotalMinutes
    if ($ageMin -lt 10) {
        Write-Log "another live run is active (lock age $([math]::Round($ageMin,1))m); exiting"
        exit 0
    }
    Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
}
Set-Content -Path $LockFile -Value (Get-Date -Format "s") -Encoding UTF8

try {
    $today = Get-Date -Format "yyyy-MM-dd"
    Write-Log "=== live $today ==="

    if (-not (Ensure-EventsApi)) {
        Write-Log "WARN continuing: the publisher can still send what is already in events.db"
    }

    $state = Read-State

    # ── 轻采集：每轮都做 ────────────────────────────────────────────
    Invoke-Py "desktop" "scripts\collect_desktop.py" @("--date", $today) | Out-Null
    # 中继上的「现在在干嘛」快照：原来只在 10 分钟的 Desktop 任务里推，
    # AFK / 当前窗口最多会旧 10 分钟。站点 NOW 每 30 秒轮询，2 分钟推一次
    # 才能让 AFK 和「现在在」跟得上。
    Invoke-Py "push-desktop" "scripts\push_desktop.py" @() | Out-Null
    Invoke-Py "phone-events" "scripts\collect_phone_events.py" @("--date", $today) | Out-Null
    # 只采集不归一化的话，blocks 表不会动，站点聚合里的分类时长会一直停在上一次
    # normalize。它只跑当天、几秒级，放在每轮里开销可以接受。
    Invoke-Py "normalize" "scripts\normalize.py" @("--date", $today) | Out-Null

    # ── 重采集：位置 / 手表 / AI 账单，按分钟节流 ──────────────────
    if (Is-Due $state.last_heavy $HeavyMinutes) {
        Invoke-Py "phone-location" "scripts\collect_phone.py" @("--date", $today) | Out-Null
        Invoke-Py "watch-pull" "scripts\collect_watch.py" @("--date", $today) | Out-Null
        Invoke-Py "watch-ingest" "middleware\ingest_gadgetbridge.py" @("--date", $today) | Out-Null
        # GPX/FIT 锻炼文件：手机 Tasker 传到中继，这里拉到 state/gpx|fit 再解析成
        # domain=fitness / metric=watch_track（地图上的虚线手表路线）。
        Invoke-Py "media-collect" "scripts\collect_media.py" @() | Out-Null
        Invoke-Py "gpx-import" "scripts\import_gpx.py" @() | Out-Null
        Invoke-Py "fit-import" "scripts\import_fit.py" @() | Out-Null
        # 中继 /watch 是飞书机器人读的那份；不推的话它会一直停在上一份。
        Invoke-Py "push-health" "scripts\push_health.py" @() | Out-Null
        Invoke-Py "ai-usage" "scripts\collect_ai_usage.py" @("--date", $today) | Out-Null
        Invoke-Py "cost-events" "scripts\cost_guard.py" @("emit-events", "--days", "1") | Out-Null
        # 当天原始 blocks → 站点七维详情的应用 / 窗口归因。只发 blocks，
        # 日记 / 事件 / 问答 / 坐标仍旧只由 23:00 那轮发。
        # 不关翻译：站点是英文界面，中文窗口标题不能原样上去（只翻新增标题）。
        if (-not $NoPublish) {
            $blocksArgs = @("--date", $today, "--kinds", "blocks")
            if ($DryRun) { $blocksArgs += "--dry-run" }
            Invoke-Py "publish-blocks" "scripts\publish_life_raw.py" $blocksArgs | Out-Null
        }
        $state.last_heavy = (Get-Date).ToString("s")
        Write-State $state
    }

    # ── 发布今天的聚合 ──────────────────────────────────────────────
    if (-not $NoPublish) {
        $publishArgs = @("--date", $today)
        if ($DryRun) { $publishArgs += "--dry-run" }
        Invoke-Py "publish-site" "scripts\publish_life_site.py" $publishArgs -ShowOutput | Out-Null

        # 「我现在在干嘛」：位置 / 电脑 / 手机 / 手表 / 实时问答
        $nowArgs = @()
        if ($DryRun) { $nowArgs += "--dry-run" }
        Invoke-Py "publish-now" "scripts\publish_live_now.py" $nowArgs | Out-Null
    }

    # ── 可选：原始归档（默认关闭）──────────────────────────────────
    if ($RawMinutes -gt 0 -and (Is-Due $state.last_raw $RawMinutes)) {
        $rawArgs = @("--date", $today)
        if ($DryRun) { $rawArgs += "--dry-run" }
        Invoke-Py "publish-raw" "scripts\publish_life_raw.py" $rawArgs | Out-Null
        $state.last_raw = (Get-Date).ToString("s")
        Write-State $state
    }

    Write-Log "=== done ==="
} finally {
    Remove-Item $LockFile -Force -ErrorAction SilentlyContinue

    # 只留 7 天日志：每 2 分钟一轮，日志增长很快
    Get-ChildItem $LogDir -Filter "live_*.log" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

exit 0
