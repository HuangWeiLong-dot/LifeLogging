# Register the LifeLogging-Live scheduled task.
#
# 每 $Minutes 分钟跑一次 scripts\run_live.ps1：
#   轻采集（电脑活动 / 手机使用）→ 发布今天到目前为止的聚合。
# 位置 / 手表 / AI 账单由 run_live.ps1 自己按 $HeavyMinutes 节流。
#
# 和 setup_scheduler.ps1 分开，避免动到已有的 Daily / Refresh / Desktop 三个任务。
# 不需要管理员（和 setup_scheduler.ps1 一样，当前用户 + RunLevel Limited 即可）。
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_live_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\setup_live_task.ps1 -Minutes 2
#
# 卸载：
#   Unregister-ScheduledTask -TaskName "LifeLogging-Live" -Confirm:$false

param(
    [int]$Minutes = 2,
    [int]$HeavyMinutes = 15,
    [string]$TaskName = "LifeLogging-Live"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "scripts\run_live.ps1"

if (-not (Test-Path $Script)) {
    Write-Host "FAIL script not found: $Script" -ForegroundColor Red
    exit 1
}
if ($Minutes -lt 1) {
    Write-Host "FAIL -Minutes must be >= 1" -ForegroundColor Red
    exit 1
}

Write-Host "-> Registering $TaskName"
Write-Host "   every $Minutes minute(s), heavy refresh every $HeavyMinutes minute(s)"
Write-Host "   script: $Script"

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`" -HeavyMinutes $HeavyMinutes" `
    -WorkingDirectory $Root

# RepetitionDuration 用 3650 天而不是 TimeSpan::MaxValue：
# 后者在部分 Windows 版本上注册会失败。
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 8) `
    -MultipleInstances IgnoreNew

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
    Write-Host "      Re-run as administrator if the policy blocks task registration." -ForegroundColor Yellow
    exit 1
}

$registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $registered) {
    Write-Host "FAIL task not found after registration" -ForegroundColor Red
    exit 1
}

Write-Host "OK   $TaskName is $($registered.State)"
Write-Host "     unregister: Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"
Write-Host "     run now:    Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "     log:        $Root\state\logs\live_<date>.log"
