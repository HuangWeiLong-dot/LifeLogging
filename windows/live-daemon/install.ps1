# 安装 / 更新 LifeLogging 桌面调度器 + 看板。
#
#   powershell -ExecutionPolicy Bypass -File windows\live-daemon\install.ps1
#
# 做四件事：
#   1. 编译 LifeLoggingLive.exe（用系统自带 csc，不装 SDK）
#   2. 在「启动」文件夹放一个快捷方式 → 登录后自动启动（无窗口，-silent）
#   3. 停用全部 LifeLogging 计划任务 —— 它们每次触发都会闪一下控制台窗口、
#      抢走鼠标焦点；现在改由这个常驻程序在进程内调度：
#         LifeLogging-Live     每 2 分钟
#         LifeLogging-Desktop  每 10 分钟
#         LifeLogging-Refresh  每 2 小时
#         LifeLogging-Daily    每天 23:00 + 登录 / 解锁补跑
#   4. 立刻启动新版并显示一次看板；之后登录启动走 -silent，只在托盘。
#
# 不需要管理员。回退：重新 Enable-ScheduledTask 那几个任务，并退出本程序。
# 卸载见同目录 README.md。

param(
    [switch]$NoStart,
    [switch]$KeepTask
)

$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
$Root = Split-Path -Parent (Split-Path -Parent $here)
$Root = (Resolve-Path $Root).Path
$exe = Join-Path $Root "LifeLoggingLive.exe"

# 更新时先停掉旧实例：exe 正在运行时 csc 写不进去。
$running = Get-Process -Name "LifeLoggingLive" -ErrorAction SilentlyContinue
if ($running) {
    foreach ($p in $running) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 700
}

Write-Host "-> building the exe"
& (Join-Path $here "build.ps1") -Root $Root
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $exe)) {
    Write-Host "FAIL build failed" -ForegroundColor Red
    exit 1
}

# 1) 启动文件夹快捷方式
$startup = [Environment]::GetFolderPath([Environment+SpecialFolder]::Startup)
$lnkPath = Join-Path $startup "LifeLogging Live.lnk"
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $exe
$lnk.Arguments = "`"$Root`" -silent"
$lnk.WorkingDirectory = $Root
$lnk.WindowStyle = 7
$lnk.Description = "LifeLogging desktop scheduler (hidden)"
$lnk.Save()
Write-Host "OK   startup shortcut: $lnkPath"

# 2) 停用全部计划任务：闪窗、抢焦点的根源。
#    留着不删，方便随时 Enable-ScheduledTask 回退。
if (-not $KeepTask) {
    $names = @(
        "LifeLogging-Live",
        "LifeLogging-Desktop",
        "LifeLogging-Refresh",
        "LifeLogging-Daily"
    )
    foreach ($name in $names) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task) {
            $wasState = $task.State
            if ($wasState -ne "Disabled") {
                Disable-ScheduledTask -TaskName $name | Out-Null
            }
            Write-Host "OK   disabled scheduled task: $name (was $wasState)"
        } else {
            Write-Host "     no $name task (fine)"
        }
    }
}

# 3) 立刻启动。这次带面板，让你直接看到看板。
#    登录时启动的是 startup 快捷方式，它带 -silent，只在托盘。
if (-not $NoStart) {
    Start-Process -FilePath $exe -ArgumentList "`"$Root`"" -WorkingDirectory $Root
    Write-Host "OK   LifeLogging started (dashboard shown once; login start is silent)"
}

Write-Host ""
Write-Host "Scheduled by the app:"
Write-Host "  LIVE      every 2 min    scripts\run_live.ps1 -HeavyMinutes 15"
Write-Host "  DESKTOP   every 10 min   scripts\push_desktop.py"
Write-Host "  REFRESH   every 2 h      scripts\run_daily.ps1 -Refresh"
Write-Host "  DAILY     daily 23:00    scripts\run_daily.ps1  (+ login / unlock catch-up)"
Write-Host ""
Write-Host "Logs:"
Write-Host "  state\logs\live-daemon.log   (job start / exit / errors; also in the dashboard)"
Write-Host "  state\logs\live_<date>.log   (each run_live.ps1 round)"
Write-Host ""
Write-Host "Rollback:"
Write-Host "  Get-Process LifeLoggingLive -ErrorAction SilentlyContinue | Stop-Process"
Write-Host "  Remove-Item `"$lnkPath`" -Force"
Write-Host "  Enable-ScheduledTask -TaskName LifeLogging-Live,LifeLogging-Desktop,LifeLogging-Refresh,LifeLogging-Daily"
