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
    [switch]$DryRun,
    # 定时刷新：等价于 -Date today -SkipAI，只是换个标签。
    # 计划任务 LifeLogging-Refresh 每隔几小时跑一次这个模式，把
    # 「双击 更新数据.cmd」自动化掉 —— 白天 Grafana 里当天的数据才是新的。
    # 它**不调 AI、不推进水位线**，所以重复跑不花钱、也不会掩盖漏掉的天。
    [switch]$Refresh,
    # 跑完不弹通知（调试用；正常运行时通知是有意要留的）。
    [switch]$NoNotify
)

# -Refresh 就是「拉今天的数据，但不做正式处理」。放在这里而不是接在
# 参数块后面几行，是因为下面所有判断都读 $Date / $SkipAI。
if ($Refresh) {
    $Date = "today"
    $SkipAI = $true
}

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

# 由 Invoke-OneDay 每天的 push-input 之后设置：这一天的输入包是不是
# `final=true` 且真的推到了中继。只有它为 true 才推进水位线。
$script:LastDayFinal = $false

function Write-Log($msg) {
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Show-Toast($title, $body) {
    # Windows 通知。**必须用 PowerShell 自己的 AppUserModelID** ——
    # 随便编一个 ID 的话系统会静默丢弃：不报错、也什么都不显示，
    # 是最容易白忙一场的坑（这个项目已经栽过好几次同类）。
    # 整个函数包在 try 里：通知失败绝不能影响流水线本身。
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        $tmpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
                    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $nodes = $tmpl.GetElementsByTagName("text")
        $nodes.Item(0).AppendChild($tmpl.CreateTextNode($title)) | Out-Null
        $nodes.Item(1).AppendChild($tmpl.CreateTextNode($body)) | Out-Null
        $toast = [Windows.UI.Notifications.ToastNotification]::new($tmpl)
        $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
    } catch {
        Write-Log "   (notification unavailable: $($_.Exception.Message))"
    }
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
    # 自评标签：把心情/体力的 1–5 分抽成 state/labels.jsonl。
    # ⚠️ 必须排在上面那步之后 —— collect-phone-events 刚把 qa-<day>.json 拉回来。
    if (-not $DryRun) {
        Invoke-Step "labels" "scripts\collect_labels.py" @() | Out-Null
        # 自由文本 → 1–5：人际/吃饭/休息/学习这四个维度没有打分题，只有一段话，
        # 用 LLM 按固定量表打分，写进 labels-llm.jsonl（**和自评分开存**）。
        # ⚠️ 这一条会花钱（一天一次调用，已有的结果直接复用）。放在 labels
        #    之后：已经是 1–5 的答案这里不再重复打一遍。
        Invoke-Step "labels-llm" "scripts\label_free_text.py" @() | Out-Null
    }
    # 手表：先把 Gadgetbridge 导出的 SQLite 从中继取回来，再读它。
    # 两步必须按这个顺序 —— ingest_gadgetbridge 读的是本地那份文件。
    Invoke-Step "collect-watch"   "scripts\collect_watch.py"   @("--date", $ds) | Out-Null
    Invoke-Step "gadgetbridge"    "middleware\ingest_gadgetbridge.py" @("--date", $ds) | Out-Null
    # 把健康日汇总推给服务器 —— 机器人跑在服务器上，看不到这台笔记本。
    # ⚠️ 必须在 ingest 之后：它读的就是刚写进 events.db 的那几行。
    Invoke-Step "push-health"     "scripts\push_health.py" | Out-Null

    # 2. 归一化
    $normalized = Invoke-Step "normalize" "scripts\normalize.py" @("--date", $ds)

    # 2a. 状态向量：把已归一化的日汇总拍平成 state/features.jsonl（一天一行）。
    #     不参与日记生成，但以后的相关性/基线/预测都只吃这一层 ——
    #     它的字段定义带版本号，改了要 bump FEATURE_VERSION。
    #     --coverage：顺手打一张「哪个源缺了」的表（传感器健康报告）。
    if (-not $DryRun) {
        $featArgs = @()
        if (-not $Refresh) { $featArgs = @("--coverage") }
        Invoke-Step "features" "scripts\features.py" $featArgs | Out-Null
        # 标签 + 证据 → state/graph.json（节点=天×维度，边分证词/统计两种）。
        # 纯本地计算、不花钱；样本不够时它会明说，不会编统计结论。
        Invoke-Step "graph" "scripts\link_graph.py" @() | Out-Null
        # 课表 vs 实际 → state/deviations.jsonl（偏离）。
        # 没有 config/timetable.yaml 时它会明说并退出，不算失败。
        Invoke-Step "routine" "scripts\routine.py" @() | Out-Null
        # 今天和你自己的常态差多少（中位数 + MAD，不预测）。
        # ⚠️ 只在正式跑（不是每 2 小时的刷新）做：拿只过了半天的今天
        #    去和完整的历史比，偏离全是假的。
        if (-not $Refresh) {
            Invoke-Step "baseline" "scripts\baseline.py" @("--date", $ds) | Out-Null
        }
    }

    # 2b. 重建长期记忆的检索索引（日记正文 + 笔记 + 问答）。
    #
    # ⚠️ 放在**归一化之后、推输入之前**：push_journal_input 要用它检索相关历史，
    #    而且必须包含刚采集到的笔记。全量重建三百来篇是毫秒级，不做增量 ——
    #    增量要维护「哪些变了」的状态，那个状态一旦和现实对不上，索引就会
    #    静默地缺内容。
    # ⚠️ 它不花 AI 的钱，所以 `-SkipAI` 时照跑（刷新也要能检索）。
    if (-not $DryRun) {
        Invoke-Step "memory-index" "scripts\memory.py" @("reindex") | Out-Null
    }

    # 3. 把「写日记需要的一切」推给服务器。
    #
    # ⚠️ 这一步**和调不调 AI 无关**（-SkipAI / -Refresh 时都要做）。服务器那边是
    #    独立的定时任务，靠这个输入包决定写不写日记 —— 本机做不做 AI 是另一回事。
    #    而且 `-Refresh`（每 2 小时）推的正是当天唯一的那份「非终版」数据。
    $script:LastDayFinal = $false
    if ($normalized -and -not $DryRun) {
        # 先删旧标记：推送脚本如果在中途崩了，绝不能读到上一次留下的
        # final=true 而把这一天误判成已终版。
        $marker = Join-Path $Root "state\journal-final-$ds.json"
        Remove-Item $marker -ErrorAction SilentlyContinue
        $pushed = Invoke-Step "push-input" "scripts\push_journal_input.py" @("--date", $ds)
        if ($pushed -and (Test-Path $marker)) {
            try {
                $j = Get-Content $marker -Raw -Encoding UTF8 | ConvertFrom-Json
                $script:LastDayFinal = ([bool]$j.final -and [bool]$j.relay_ok)
                if (-not $script:LastDayFinal) {
                    Write-Log "   day $ds is not final yet (final=$($j.final), relay_ok=$($j.relay_ok)); watermark will not advance"
                }
            } catch {
                Write-Log "   WARN could not read $marker; treating as not final"
            }
        } else {
            Write-Log "   WARN push-input failed or wrote no final marker; watermark will not advance"
        }
    }

    # ⚠️ 「取日记」**不在这里**，见日循环之后那一段。把那一步放进来的话：
    #    取不到时它会退出码 0（「服务器还没生成」是正常状态），于是这一天被判定
    #    成功、水位线前进 —— 而那份几小时后才生成的日记再也没人去取。
    return $normalized
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
    # 被挡住**必须**弹通知：这是最需要你知道的一种失败，
    # 而且它的表现是「一切安静」—— 前一天那条路径就是静默丢了一整天。
    if (-not $NoNotify) {
        $why = Get-Content (Join-Path $LogDir "events_api.err.log") -Tail 1 -Encoding UTF8 -ErrorAction SilentlyContinue
        Show-Toast "LifeLogging 被挡住了" "事件表连不上，今天没处理。$why"
    }
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
        # ⚠️ 不要 exit。日循环后面的「取日记 / 生成提问 / 回收答案」仍然要跑：
        #    前一天 23:10 才生成的复盘、服务器晚写好的日记，都靠这些步骤补上。
        $targets = @()
    } else {
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
}

# 给「可见性」用的模式标签 —— 会写进 state\last_run.md 和系统通知。
$modeLabel = if ($Refresh) { "定时刷新" } elseif ($manual) { "手动补跑" } else { "自动" }

# ── 回拉前一天事件 ──────────────────────────────────────────────────
#
# 前一天的【今日复盘】是 23:10 左右才写下的，而当天 23:00 的采集已经过去了。
# 不补拉的话，复盘笔记永远进不了 events.db / memory，第二天检索不到。
if (-not $DryRun) {
    $prevDay = $today.AddDays(-1).ToString("yyyy-MM-dd")
    Invoke-Step "collect-phone-events-prev" "scripts\collect_phone_events.py" @("--date", $prevDay) | Out-Null
    if ($targets.Count -eq 0) {
        # 没有新的一天要处理时，日循环里的 memory-index 不会跑；这里补一次。
        Invoke-Step "memory-index-prev" "scripts\memory.py" @("reindex") | Out-Null
    }
}

Write-Log ("=" * 60)

$okDays = 0
$failDays = @()

foreach ($d in $targets) {
    if (Invoke-OneDay $d) {
        $okDays++
        if ($manual) {
            # 手动补跑不碰水位线（保持原行为）
        } elseif ($DryRun) {
            Write-Log "   $($d.ToString('yyyy-MM-dd')): dry-run; watermark not advanced"
        } elseif ($script:LastDayFinal) {
            Set-LastProcessed $d
        } else {
            # 23:00 那次 final=false；停在这里，等午夜后的运行补推终版。
            Write-Log "   $($d.ToString('yyyy-MM-dd')): not final yet; watermark stays put (will retry)"
        }
    } else {
        $failDays += $d.ToString("yyyy-MM-dd")
    }
}

# ── 取回服务器写好的日记 ────────────────────────────────────────────
#
# 放在日循环**外面**，而且往前扫几天，有两个理由：
#
#   1. AI 那一步搬到服务器上了（scripts/close_day.py），它什么时候生成不受本机
#      控制 —— 本机完全可能醒得比它早。
#   2. 水位线管的是「采集有没有做完」，**不是**「AI 日记有没有写好」。把两者绑在
#      一起，取不到时就只能二选一：要么谎报成功（那份日记永远丢了），要么谎报
#      失败（每天早上报红，很快你就不看了）。
#
# 所以这里和 cost-events / questions 一样尽力而为。脚本自身幂等
# （state/journal-pulled-<day>.txt 记着已经组装过哪一版），重复运行不会重复写。
# ⚠️ 这一步**不受 -SkipAI 影响** —— `-SkipAI` 的意思是「别花 AI 的钱」，而取回
#    一份已经生成好的正文一分钱不花。挡住它只会让 更新数据.cmd 永远拿不到日记。
if (-not $DryRun) {
    Invoke-Step "journal-pull" "scripts\pull_journal_output.py" @("--days", "3") | Out-Null

    # 服务器上花的 AI 钱也要并进本机台账。
    #
    # ⚠️ 不做这一步的话，Grafana 的「今日 AI 花费」会**少掉日记那笔** ——
    #    面板读的是本机事件表里的 ai/daily_cost，而那个数是从本机台账算的，
    #    而日记现在花的是服务器的钱。两边都不会报错，只是数字小了。
    #    同样不受 -SkipAI 影响（合并不花 AI 的钱）。
    Invoke-Step "ledger-merge" "scripts\merge_ledger.py" @() | Out-Null
}

# ── 学着认出新应用 ─────────────────────────────────────────────────
#
# 扫最近 7 天的 timeline，把「时间花了不少、但规则表没认出来」的应用问一次
# 模型，答案缓存进 state\app_map.json —— 之后就和手写规则一样免费、确定。
#
# ⚠️ 放在日循环**外面**：它是按「最近 N 天」学，不是按天学。补跑 5 天不该
#    学 5 遍。
# ⚠️ `-SkipAI` 时跳过（它就是花钱的那部分）。所以白天的刷新不学，
#    23:00 那次正式处理才学 —— 一天最多一次，且每次最多学 8 个。
if (-not $SkipAI -and -not $DryRun) {
    Invoke-Step "learn-apps" "scripts\learn_apps.py" @("--days", "7") | Out-Null
}

# ── 每日提问：生成新的 + 回收答回来的 ───────────────────────────────
#
# 两件事都必须放在日循环**外面**，而且只针对最近的那一天：
#   · 生成问题要花钱（一次 LLM 调用），补跑 5 天不该生成 5 份过期问题；
#     何况中继只认**最新**那一份，生成一堆等于互相覆盖。
#   · 回收答案只关心「刚过去的那一天问了什么」。
# 有 targets 就针对最后一天生成；没有（今天没有新数据要处理）就针对昨天，
# 把漏掉的问责批补上。生成脚本自己会跳过已经存在的文件，所以多跑几次无副作用。
$qDayDate = if ($targets.Count -gt 0) { $targets[-1] } else { $today.AddDays(-1) }
# ⚠️ 必须 .ToString("yyyy-MM-dd")。直接把 DateTime 插进字符串会走本地化
#    格式（'10/14/2026 12:00:00 AM'），Python 那边 date.fromisoformat 直接炸。
$qDay = $qDayDate.ToString("yyyy-MM-dd")
# 没有 targets 时只在**上午**补生成：下午/晚上生成会把「昨天的题」推到明天
# 早上，题号仍是昨天的，读起来就是过期题。23:00 那次有 targets（今天），不受影响。
$shouldGenerate = ($targets.Count -gt 0 -or (Get-Date).Hour -lt 12)
if ($shouldGenerate -and -not $SkipAI -and -not $DryRun) {
    Invoke-Step "questions" "scripts\generate_questions.py" @("--date", $qDay) | Out-Null
}

# 答案今早才答回来：自动模式下永远回收「昨天」的问题。
# 手动补跑保留原来的 targets[-1]-1 —— 补跑历史时，问的是那一天的前一天。
$aDayDate = if ($manual) { $targets[-1].AddDays(-1) } else { $today.AddDays(-1) }
$aDay = $aDayDate.ToString("yyyy-MM-dd")
if (-not $DryRun) {
    Invoke-Step "answers" "scripts\apply_answers.py" @("--date", $aDay) | Out-Null
}

# ── 周整合：把刚过去的一周压成一份回顾 ───────────────────────────────
#
# 放在日循环**外面**，而且不依赖 $targets —— 一周结束时不一定有新的一天要处理。
# 只处理**已经过完**的 ISO 周；已经有回顾文件就跳过。
#
# ⚠️ 产物有两份：vault\07-Reviews\<年>-W<周>.md 给人看，
#    state\weeks\<年>-W<周>.json 里的结构化事实留给模型用（第三层的画像吃它）。
# ⚠️ `-SkipAI` 时跳过 —— 它是一次 LLM 调用。
if (-not $SkipAI -and -not $DryRun) {
    Invoke-Step "weekly" "scripts\weekly_review.py" @("--max-weeks", "4") | Out-Null
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

# ── 发布脱敏汇总到个人站点（liveinpassion.me/life）─────────────────
#
# 只发聚合数字：分类时长、步数/心率/跑步、城市级位置、手机计数、AI 花费。
# 不发坐标、轨迹、窗口标题/URL、包名、余额。字段白名单在 Worker 侧再校验一次。
#
# 放在日循环外面：每晚重发近 30 天，幂等 upsert，顺带自愈漏跑的日子。
# 白天 `-Refresh` / 更新数据.cmd 会带 -SkipAI，此时不发半天的数据。
if (-not $DryRun -and -not $SkipAI) {
    Invoke-Step "publish-life" "scripts\publish_life_site.py" @("--days", "30") | Out-Null
    # 全量原始数据（日记 / 事件 / 随手记 / 任务 / 问答）；只遮盖凭证参数
    Invoke-Step "publish-life-raw" "scripts\publish_life_raw.py" @("--days", "30") | Out-Null
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

# ── 可见性 ──────────────────────────────────────────────────────────
#
# 这个脚本平时跑在隐藏窗口里（计划任务拉起），跑完什么都不留 —— 唯一线索是
# 去翻 state\logs\。于是「它到底跑没跑」全靠猜。
#
# 更要命的是**最该跑成的那次最不容易跑成**：每日 23:00 那个触发器需要机器
# 在 23:00 开着，而这台笔记本那时多半已经关机或睡眠（WakeToRun=False，有意
# 不唤机器）。实测 10-14 那天 22:18 就关机了，23:00 那次根本没机会跑。
# 剩下的自动运行全是白天的解锁触发 —— 按水位线设计它们只处理到「昨天」，
# 所以永远印 Nothing to do，看不出任何动静。
#
# 所以每次运行都留两样东西：
#   · state\last_run.md —— 一页摘要 + 最近若干次运行的时间线，随时能看
#   · 一个系统通知 —— 只在**真处理了东西**或**失败**时弹。定时刷新
#     （每几小时一次）不弹，否则一天弹十几次就成了噪音
if (-not $DryRun) {
    $lastWm = Get-LastProcessed
    $wmText = if ($lastWm) { $lastWm.ToString('yyyy-MM-dd') } else { '(未设置)' }
    $targetText = if ($targets.Count -gt 0) {
        ($targets | ForEach-Object { $_.ToString('yyyy-MM-dd') }) -join ', '
    } else { '无' }
    $resultText = if ($failDays.Count -gt 0) { "失败：$($failDays -join ', ')" } else { "$okDays 天成功" }

    # 最近几次运行直接扫日志文件（文件名自带时间戳），再抓每个日志里那一行
    # 结论。比另建状态文件可靠 —— 不会和真正发生过的运行脱节。
    $hist = @()
    foreach ($f in (Get-ChildItem $LogDir -Filter "run_*.log" |
                    Sort-Object LastWriteTime -Descending | Select-Object -First 15)) {
        $verdict = Get-Content $f.FullName -Encoding UTF8 -ErrorAction SilentlyContinue |
                   Where-Object { $_ -match 'Done --|BLOCKED|Nothing to do' } |
                   Select-Object -Last 1
        if (-not $verdict) { $verdict = '(未到结论就退出)' }
        $hist += "| $($f.LastWriteTime.ToString('MM-dd HH:mm')) | $("$verdict".Trim()) |"
    }

    $md = @(
        "# 最后一次运行",
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| 时间 | $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') |",
        "| 模式 | $modeLabel |",
        "| 目标 | $targetText |",
        "| 结果 | $resultText |",
        "| 水位线 | $wmText |",
        "| 日志 | ``$LogFile`` |",
        "",
        "> 每次运行后重写。这一页只回答「跑没跑、处理了哪天」；细节翻日志。",
        "",
        "## 最近运行",
        "",
        "| 时间 | 结论 |",
        "|---|---|"
    ) + $hist + @("")
    Set-Content -Path (Join-Path $Root "state\last_run.md") -Value ($md -join "`r`n") -Encoding UTF8

    if ((-not $NoNotify) -and ((-not $Refresh) -or $failDays.Count -gt 0)) {
        Show-Toast "LifeLogging · $modeLabel" "$resultText ｜ 目标 $targetText"
    }
}
