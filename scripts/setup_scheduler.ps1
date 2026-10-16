# Register the Windows scheduled tasks
#
# 注册**三个**任务：
#   LifeLogging-Daily    每日 $Time + 解锁时 —— 正式处理（含 AI 日记、推水位线）
#   LifeLogging-Refresh  每 $RefreshHours 小时 —— 只拉今天的数据，不调 AI、不动水位线
#   LifeLogging-Desktop  每 $DesktopMinutes 分钟 —— 把「电脑上今天到目前」推给服务器
#
# 以**管理员身份**运行 PowerShell：
#   powershell -ExecutionPolicy Bypass -File scripts\setup_scheduler.ps1
#
# 卸载：
#   Unregister-ScheduledTask -TaskName "LifeLogging-Daily"   -Confirm:$false
#   Unregister-ScheduledTask -TaskName "LifeLogging-Refresh" -Confirm:$false
#   Unregister-ScheduledTask -TaskName "LifeLogging-Desktop" -Confirm:$false

param(
    [string]$Time = "23:00",
    [string]$TaskName = "LifeLogging-Daily",
    # 定时刷新的间隔（小时）。这个任务做的是 更新数据.cmd 那件事 ——
    # 把当天的数据拉下来让 Grafana 保持新鲜，不调 AI（所以几乎零成本）。
    [int]$RefreshHours = 2,
    [string]$RefreshTaskName = "LifeLogging-Refresh",
    # 电脑活动快照的间隔（分钟）。见 scripts/push_desktop.py ——
    # 让飞书机器人在笔记本睡着之后还能答「电脑上最后在干什么」。
    # 10 分钟是个折中：更密没有意义（人不会每 3 分钟换一件事），
    # 更疏则笔记本一睡，最后那份快照就偏得很远。
    [int]$DesktopMinutes = 10,
    [string]$DesktopTaskName = "LifeLogging-Desktop"
)

$ErrorActionPreference = "Stop"

# 是否管理员 —— 只影响提示，**不再是硬性门槛**。
# 实测：注册一个「当前用户身份 + RunLevel Limited」的任务不需要提权。
# 原来那个 `if (-not $isAdmin) { exit 1 }` 是无谓地挡人。
# 注意：不要写成 C# 风格的 ([WindowsPrincipal] [WindowsIdentity]::GetCurrent()).IsInRole(...)
# —— 那在 PowerShell 里不是合法表达式。老老实实分三步。
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
$isAdmin = $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($isAdmin) {
    Write-Host "-> Running as administrator" -ForegroundColor DarkGray
} else {
    Write-Host "-> Running as a normal user (usually enough; verified)" -ForegroundColor DarkGray
}

$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "scripts\run_daily.ps1"
# 推送脚本直接用 venv 的 python 跑（不走 run_daily.ps1 —— 那个是「按天」的
# 流水线，带水位线和 -Date today 语义，塞进一个每 10 分钟的任务里不对路）。
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
$PushScript = Join-Path $Root "scripts\push_desktop.py"

if (-not (Test-Path $Script)) {
    Write-Host "FAIL script not found: $Script" -ForegroundColor Red
    exit 1
}

Write-Host "-> Registering scheduled task"
Write-Host "   Name: $TaskName"
Write-Host "   Time: daily at $Time"
Write-Host "   Script: $Script"

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`"" `
    -WorkingDirectory $Root

# 只构造 23:00 的每日触发器。
# 解锁触发器**不在这里**构造 —— 它必须走 XML，原因见下面「注册之后」那一节。
$trigger = New-ScheduledTaskTrigger -Daily -At $Time

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

# 跑在用户身份下（需要读到用户目录里的 screenpipe 数据库）
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null
} catch {
    Write-Host "FAIL registration failed: $_" -ForegroundColor Red
    Write-Host
    Write-Host "If this is a permission problem, re-run as administrator:" -ForegroundColor Yellow
    Write-Host "  powershell -File `"$PSCommandPath`" -Time `"$Time`""
    exit 1
}

# ── 注册之后：补上「解锁时」触发器 ──────────────────────────────────
#
# **为什么需要它** —— 单靠 23:00 那个触发器有一个洞：
#
#   这台机器用的是**传统 S3 睡眠**（`powercfg /a` 里 S0 Low Power Idle 不可用），
#   而本任务 WakeToRun = False，也就是**唤不醒机器**。
#   所以 23:00 那一刻如果机器在睡或关着，任务不会跑，也不会被叫醒。
#
#   原来指望的是 StartWhenAvailable「错过后补跑」，但那要等**下一次开机**。
#   机器如果只是合盖睡了一晚、并没有重启，就没有这个触发点 ——
#   那一天的记录会被静默跳过。
#   （实测：这台机器连续运行 10 天没重启过；而 2.5 个月里 100 次睡眠尝试
#     没有一次超过 60 秒 —— 它其实一直醒着。任务能跑是碰巧，不是机制生效。）
#
# 加一个解锁触发器就补上了：合盖睡一晚、早上开盖解锁 → 任务触发。
# 这正是 run_daily.ps1 那套水位线逻辑的用武之地 ——
#   它幂等、无事可做会立刻 exit 0，所以在「每次解锁」这种高频触发下
#   几乎零成本；真正漏掉的那几天，会在下一次解锁时被补回来。
#
# **为什么走 XML 而不是构造触发器对象**
#
#   实测：`New-CimInstance -CimClass MSFT_TaskSessionStateChangeTrigger -ClientOnly`
#   造出来的实例 Register-ScheduledTask 一律拒收 ——
#   `0x80070057 The parameter is incorrect`，带不带属性、单独传还是和每日触发器
#   一起传，四种组合全试过，全失败（见 docs/运维手册.md 的排障记录）。
#   而 `New-ScheduledTaskTrigger` 这个 cmdlet 压根不支持会话状态变更触发器。
#
#   所以：先按对象式注册好任务，再导出 XML、插入一段触发器、用 XML 重新注册。
#   好处是结果可以用 Export-ScheduledTask 直接核对，不是黑箱。
$xmlText = Export-ScheduledTask -TaskName $TaskName
if (-not $xmlText) {
    Write-Host "FAIL could not export the task we just registered" -ForegroundColor Red
    exit 1
}

# 去掉 <?xml ... ?> 声明：导出的是 UTF-16 声明，而我们传的是 .NET 字符串，
# 声明和实际编码对不上会让解析失败。
$xmlText = $xmlText -replace '^\s*<\?xml[^>]*\?>\s*', ''

# 用 SID 而不是 DOMAIN\User：和 Export 出来的 <UserId> 保持同一种写法。
$sid = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value

# ⚠️ StateChange 写**符号名** SessionUnlock，别写数字。
#    数字是个坑：CIM 里 SessionUnlock 是 **8**，不是大多数人以为的 6 ——
#    注册完 `(Get-ScheduledTask -TaskName '...').Triggers` 报的 StateChange 就是 8。
#    写符号名就不用管这码事。（元素顺序 Task Scheduler 自己会规范化，
#    注册后 Export 出来是 Delay / StateChange / UserId。）
# ⚠️ Delay 不是可有可无：任务设了 RunOnlyIfNetworkAvailable = True，
#    而解锁那一刻 Wi-Fi / Tailscale 往往还没就绪。给 30 秒让网络起来。
$unlockTriggerXml = @(
    "  <SessionStateChangeTrigger>"
    "    <Enabled>true</Enabled>"
    "    <Delay>PT30S</Delay>"
    "    <UserId>$sid</UserId>"
    "    <StateChange>SessionUnlock</StateChange>"
    "  </SessionStateChangeTrigger>"
) -join "`r`n"

$closeIdx = $xmlText.IndexOf("</Triggers>")
if ($closeIdx -lt 0) {
    Write-Host "FAIL no </Triggers> in the exported task XML" -ForegroundColor Red
    exit 1
}
$xmlText = $xmlText.Substring(0, $closeIdx) + $unlockTriggerXml + "`r`n" + $xmlText.Substring($closeIdx)

try {
    Register-ScheduledTask -Xml $xmlText -TaskName $TaskName -Force | Out-Null
} catch {
    Write-Host "FAIL could not add the unlock trigger: $_" -ForegroundColor Red
    Write-Host "     The daily 23:00 trigger is registered and working;" -ForegroundColor Yellow
    Write-Host "     a missed run will then only be caught up at the next boot." -ForegroundColor Yellow
    exit 1
}

$registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$trigCount = 0
if ($registered) { $trigCount = @($registered.Triggers).Count }
if ($trigCount -lt 2) {
    Write-Host "FAIL registered, but only $trigCount trigger(s) came back -- expected 2" -ForegroundColor Red
    exit 1
}

# ── 第二个任务：每 $RefreshHours 小时刷新「今天」 ────────────────────
#
# **为什么需要它**
#
#   LifeLogging-Daily 有个绕不过去的性质：白天它必然什么都不做。
#   run_daily.ps1 的 `includeToday = (小时 >= 23)`,也就是**今天只在 23:00
#   之后才被正式处理** —— 否则早上补跑会把「只用了半天数据的一天」标成
#   已完成，那一整天就再也不会重跑。
#
#   于是：白天每次解锁触发的运行都印 `Nothing to do`（确实执行了，只是没
#   事可做），而唯一会产出当天数据的 23:00 那次，又因为机器那时常常已经
#   关机/睡眠而跑不成。净效果是**当天的数据永远不会自己出现**，想看得
#   手动双击 更新数据.cmd。
#
#   这个任务就是把那次双击自动化：每 $RefreshHours 小时跑一次 `-Refresh`
#   （即 `-Date today -SkipAI`）—— 只拉今天、写当天的时间块、不调 AI、
#   不推进水位线。所以重复跑既不花钱也不会掩盖漏掉的天。
#
#   两个任务分工明确：
#     Refresh  白天保持当天数据新鲜（幂等，随便跑）
#     Daily    23:00/解锁 正式处理 + 生成日记 + 推进水位线
$refreshAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`" -Refresh" `
    -WorkingDirectory $Root

# `-Once -At 今天零点` + RepetitionInterval：这是「每隔 N 小时、无限重复」
# 的标准写法。**不要**给 RepetitionDuration —— 省略它 Task Scheduler 才当
# 无限重复；给一个值反而会在到期后停下。注册完下面会导出 XML 核对一次。
$refreshTrigger = New-ScheduledTaskTrigger `
    -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Hours $RefreshHours)

try {
    Register-ScheduledTask `
        -TaskName $RefreshTaskName `
        -Action $refreshAction `
        -Trigger $refreshTrigger `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null
} catch {
    Write-Host "FAIL could not register '$RefreshTaskName': $_" -ForegroundColor Red
    Write-Host "     '$TaskName' is registered and working; only the periodic refresh is missing." -ForegroundColor Yellow
    exit 1
}

# 核对重复间隔真的落进去了 —— 这个参数的失败方式是「任务注册成功但只跑一次」，
# 不核对的话要等半天才发现它根本没重复。
$rt = Get-ScheduledTask -TaskName $RefreshTaskName
$rep = @($rt.Triggers)[0].Repetition
if (-not $rep -or -not $rep.Interval) {
    Write-Host "FAIL '$RefreshTaskName' registered but has no repetition interval." -ForegroundColor Red
    exit 1
}

# ── 第三个任务：电脑活动快照（每 $DesktopMinutes 分钟）─────────────
#
# 为什么单独一个任务、不走 run_daily.ps1：
#   run_daily 是**按天**的流水线（水位线、-Date today、补跑逻辑），
#   而这件事是「每十分钟看一眼现在」。两者的节奏和失败方式都不一样。
#   它本身也很轻：读一次 ActivityWatch 的本地接口 + 一个小 POST。
$desktopAction = New-ScheduledTaskAction `
    -Execute $Py `
    -Argument "`"$PushScript`"" `
    -WorkingDirectory $Root

$desktopTrigger = New-ScheduledTaskTrigger `
    -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes $DesktopMinutes)

try {
    Register-ScheduledTask `
        -TaskName $DesktopTaskName `
        -Action $desktopAction `
        -Trigger $desktopTrigger `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null
} catch {
    Write-Host "FAIL could not register '$DesktopTaskName': $_" -ForegroundColor Red
    Write-Host "     前两个任务已经注册好了，少的只是电脑活动快照。" -ForegroundColor Yellow
    exit 1
}

# 同样核对一次 —— 这个参数的失败方式和上面那个一模一样：注册成功但只跑一次。
$dt = Get-ScheduledTask -TaskName $DesktopTaskName
$drep = @($dt.Triggers)[0].Repetition
if (-not $drep -or -not $drep.Interval) {
    Write-Host "FAIL '$DesktopTaskName' registered but has no repetition interval." -ForegroundColor Red
    exit 1
}

Write-Host "OK  Registered" -ForegroundColor Green
Write-Host "    Triggers: $trigCount  (daily $Time  +  unlock, 30s delay)"
Write-Host "    Refresh:  every $($rep.Interval) (=$RefreshHours h), task '$RefreshTaskName'"
Write-Host "    Desktop:  every $($drep.Interval) (=$DesktopMinutes min), task '$DesktopTaskName'"
Write-Host
Write-Host "Check:"
Write-Host "  (Get-ScheduledTask -TaskName '$TaskName').Triggers"
Write-Host "  Export-ScheduledTask -TaskName '$TaskName' | Select-String SessionState"
Write-Host "  (Get-ScheduledTask -TaskName '$RefreshTaskName').Triggers   # 重复间隔"
Write-Host "  (Get-ScheduledTask -TaskName '$DesktopTaskName').Triggers   # 重复间隔"
Write-Host
Write-Host "Trigger them once to test:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Start-ScheduledTask -TaskName '$RefreshTaskName'"
Write-Host "  Start-ScheduledTask -TaskName '$DesktopTaskName'"
Write-Host
Write-Host "先手动验一次电脑快照推得出去（会打印它到底推了什么）："
Write-Host "  .venv\Scripts\python.exe scripts\push_desktop.py --dry-run"
Write-Host
Write-Host "Is it actually running? Open state\last_run.md -- it lists the recent runs."
Write-Host
Write-Host "WARN  A missed run is caught up at the next boot OR the next unlock,"
Write-Host "      but only within MaxBackfillDays (14) -- anything older must be filled in by hand:"
Write-Host "    python scripts\run_daily.ps1 -Date 2026-10-12"
