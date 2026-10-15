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
    # 手机**使用**事件（解锁/切应用）走服务器中继 —— 手机直连本机会丢数据，
    # 因为这台笔记本一天只醒几小时。跟 collect-phone 拉位置是对称的。
    Invoke-Step "collect-phone-events" "scripts\collect_phone_events.py" @("--date", $ds) | Out-Null
    # 手表：先把 Gadgetbridge 导出的 SQLite 从中继取回来，再读它。
    # 两步必须按这个顺序 —— ingest_gadgetbridge 读的是本地那份文件。
    Invoke-Step "collect-watch"   "scripts\collect_watch.py"   @("--date", $ds) | Out-Null
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

# ── 保证事件表服务在跑 ──────────────────────────────────────────────
# 它原本没有开机自启 —— 机器重启后流水线会全线失败（采集脚本 ping 不通就中止），
# 而失败信息只是「事件表服务没在跑」，看不出是「因为重启了」。
# 与其依赖 NSSM 装服务（要管理员），不如让流水线自己拉起它：
# 这样计划任务就是自足的，重启后第二天照常工作。
function Ensure-EventsApi {
    # 地址和端口从 config.yaml 读。
    # ⚠️ 不能用 Select-String 抓 "127.0.0.1:数字" 再取第一条 ——
    #    config 里 activitywatch 的 api_base(http://127.0.0.1:5600) 排在
    #    events_api 前面，取第一条会拿到 5600。让 Python 按字段名读，别靠顺序。
    #
    # ⚠️ host 也要从配置读，不能写死 127.0.0.1：手机（Tasker）要上报的话
    #    必须绑到 Tailscale 地址上，那时候健康检查也得查同一个地址 ——
    #    绑了 100.x 之后 127.0.0.1 是连不上的，写死会让服务「起得来但判定为没起」。
    $apiHost = "127.0.0.1"
    $port = 18787
    try {
        $parsed = & $Py -c "import sys;sys.path.insert(0,'scripts');from urllib.parse import urlparse;from common import load_config;u=urlparse(load_config()['sources']['events_api']['api_base']);print((u.hostname or '127.0.0.1') + ' ' + str(u.port or 18787))"
        if ($parsed) {
            $parts = "$parsed".Trim() -split '\s+'
            if ($parts.Count -ge 2 -and $parts[1] -match '^\d+$') {
                $apiHost = $parts[0]
                $port = [int]$parts[1]
            }
        }
    } catch {
        Write-Log "   WARN could not read the events API host/port from config; using ${apiHost}:$port"
    }
    $health = "http://${apiHost}:$port/health"

    # ⚠️ 健康检查必须放在**解析之后**。
    #    原来这里写死 "http://127.0.0.1:8787/health" 放在函数最前面，
    #    一旦 api_base 改成 Tailscale 地址，第一次检查必然失败 ->
    #    每次都去拉一个重复实例（端口被占，它其实起不来）->
    #    第二次检查才发现服务本来就在跑。日志里会看到假的「events API is down」。
    try {
        $r = Invoke-WebRequest -Uri $health -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -eq 200) {
            Write-Log "   events API already running"
            return $true
        }
    } catch { }

    Write-Log "   events API is down -- starting it"

    # ⚠️ 一定要把 uvicorn 的输出重定向到文件。
    #    原来用 -WindowStyle Hidden 起进程，stderr 直接丢掉 —— 服务起不来时
    #    日志里只剩一句「did not come up within 12s」，看不出真正原因。
    #    2026-10-15 那次就是：真因是端口被系统划进了保留段、bind 报
    #    WinError 10013，但必须手动在前台跑一遍才能看见。别再让错误信息消失。
    $apiOutLog = Join-Path $LogDir "events_api.out.log"
    $apiErrLog = Join-Path $LogDir "events_api.err.log"

    Start-Process -FilePath $Py `
        -ArgumentList @("-m", "uvicorn", "middleware.events_api:app",
                        "--host", "$apiHost", "--port", "$port", "--log-level", "warning") `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput $apiOutLog -RedirectStandardError $apiErrLog

    # 用秒表计时而不是写死「12s」：24 次 × 500ms 只是名义值，
    # 实际每次 Invoke-WebRequest 都可能额外阻塞，日志里的时间和代码对不上。
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    for ($i = 0; $i -lt 24; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $r = Invoke-WebRequest -Uri $health -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            if ($r.StatusCode -eq 200) {
                $sw.Stop()
                Write-Log "   events API is up (port $port, $($sw.Elapsed.TotalSeconds.ToString('0.0'))s)"
                return $true
            }
        } catch { }
    }
    $sw.Stop()

    Write-Log "   WARN events API did not come up within $($sw.Elapsed.TotalSeconds.ToString('0.0'))s on ${apiHost}:$port"
    Write-Log "        uvicorn said (state\logs\events_api.err.log):"
    if (Test-Path $apiErrLog) {
        Get-Content $apiErrLog -Tail 12 -Encoding UTF8 | ForEach-Object { Write-Log "          $_" }
    } else {
        Write-Log "          (no log written -- the process never got far enough to log)"
    }
    return $false
}

# 放在这里而不是日循环里 —— 「今天无事可做」的提前 exit 会跳过日循环，
# 那样每次重开机后如果当天没数据要处理，服务就永远起不来。
# 位置必须在函数定义之后：PowerShell 自上而下执行。
#
# 起不来就**整个 run 失败**，不要继续往下跑。
# 采集脚本把「连不上事件表」当降级处理、照样 exit 0（那对「手表没戴」是
# 对的，对「落盘的地方没了」是错的），于是流水线会印「Done -- succeeded」、
# 水位线照样前进 —— 而库里一行都没有，这一天再也不会被重跑。
# 宁可让计划任务报红，也不要静默丢一天。
if (-not (Ensure-EventsApi)) {
    Write-Log "BLOCKED events API is not reachable -- refusing to process any day."
    Write-Log "        Collectors treat a missing event table as a soft failure and exit 0,"
    Write-Log "        so the day would be reported done with nothing written, and the"
    Write-Log "        watermark would step past it -- losing that day for good."
    Write-Log "        state\last_processed.json was NOT touched. Fix the service, then rerun."
    exit 1
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

# ── 每日花费写进事件表（Grafana 的「AI 花费」面板读它）──────────────
#
# 每次调用的花费原本只记在 state/cost_ledger.jsonl，而 Grafana 挂的是
# state/events.db —— JSONL 那边它一个字都读不到。所以那条链路是断的，
# 面板上的「每日 AI 花费」永远是空的。
# 这里把台账**按天合计**写成 ai/daily_cost 事件补上。
#
# 放在日循环**外面**：它一次重写台账里的每一天（--days 90），
# 处理 3 天补齐也只需要跑一次。
Invoke-Step "cost-events" "scripts\cost_guard.py" @("emit-events", "--days", "90") | Out-Null

# ── AI 用量（Claude Code + DeepSeek 平台账单 + 余额）────────────────
#
# 台账只管本流水线自己那点调用（一天约 ¥0.02）。真正花掉的钱在 Claude Code
# 那边 —— 同一个 key、同一个余额，但它不往我们的台账写任何东西。
# 这一步把三方数据补齐，让「今日 AI 花费」不再只显示 1% 的支出。
#
# 拿不到 userToken 时会优雅降级：仍然写 Claude Code 的分布和余额快照，
# 只是没有精确金额。
Invoke-Step "ai-usage" "scripts\collect_ai_usage.py" @("--days", "3") | Out-Null

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
