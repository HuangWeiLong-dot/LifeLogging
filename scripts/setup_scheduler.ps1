# Register the Windows scheduled task: run the pipeline daily at 23:00
#
# 以**管理员身份**运行 PowerShell：
#   powershell -ExecutionPolicy Bypass -File scripts\setup_scheduler.ps1
#
# 卸载：
#   Unregister-ScheduledTask -TaskName "LifeLogging-Daily" -Confirm:$false

param(
    [string]$Time = "23:00",
    [string]$TaskName = "LifeLogging-Daily"
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

Write-Host "OK  Registered" -ForegroundColor Green
Write-Host "    Triggers: $trigCount  (daily $Time  +  unlock, 30s delay)"
Write-Host
Write-Host "Check:"
Write-Host "  (Get-ScheduledTask -TaskName '$TaskName').Triggers"
Write-Host "  Export-ScheduledTask -TaskName '$TaskName' | Select-String SessionState"
Write-Host
Write-Host "Trigger it once to test:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host
Write-Host "WARN  A missed run is caught up at the next boot OR the next unlock,"
Write-Host "      but only within MaxBackfillDays (14) -- anything older must be filled in by hand:"
Write-Host "    python scripts\run_daily.ps1 -Date 2026-10-12"
