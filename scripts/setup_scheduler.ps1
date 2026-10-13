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

Write-Host "OK  Registered" -ForegroundColor Green
Write-Host
Write-Host "Check:"
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host
Write-Host "Trigger it once to test:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host
Write-Host "WARN  StartWhenAvailable means a missed run fires after the next boot,"
Write-Host "      but if the machine was off for over a day, the catch-up still targets today -- missing days must be filled in by hand:"
Write-Host "    python scripts\run_daily.ps1 -Date 2026-10-12"
