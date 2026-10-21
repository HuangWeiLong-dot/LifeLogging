# 安装 / 更新 LifeLoggingLive 守护程序。
#
#   powershell -ExecutionPolicy Bypass -File windows\live-daemon\install.ps1
#
# 做四件事：
#   1. 编译 LifeLoggingLive.exe（如果没有）
#   2. 在「启动」文件夹放一个快捷方式 → 登录后自动启动（无窗口）
#   3. 停用 LifeLogging-Live 计划任务（它每 2 分钟拉 PowerShell，就是那个闪窗）
#   4. 把 LifeLogging-Desktop 任务改成 pythonw.exe（另一个每 10 分钟的闪窗源）
#
# 不需要管理员。卸载见同目录 README.md。

param(
    [switch]$NoStart,
    [switch]$KeepTask
)

$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
$Root = Split-Path -Parent (Split-Path -Parent $here)
$Root = (Resolve-Path $Root).Path
$exe = Join-Path $Root "LifeLoggingLive.exe"

if (-not (Test-Path $exe)) {
    Write-Host "-> building the exe first"
    & (Join-Path $here "build.ps1") -Root $Root
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $exe)) {
        Write-Host "FAIL build failed" -ForegroundColor Red
        exit 1
    }
}

# 1) 启动文件夹快捷方式
$startup = [Environment]::GetFolderPath([Environment+SpecialFolder]::Startup)
$lnkPath = Join-Path $startup "LifeLogging Live.lnk"
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $exe
$lnk.Arguments = "`"$Root`""
$lnk.WorkingDirectory = $Root
$lnk.WindowStyle = 7
$lnk.Description = "LifeLogging live pipeline (hidden)"
$lnk.Save()
Write-Host "OK   startup shortcut: $lnkPath"

# 2) 停用旧的 Live 计划任务（避免两份同时跑，也去掉每 2 分钟的闪窗）
if (-not $KeepTask) {
    $task = Get-ScheduledTask -TaskName "LifeLogging-Live" -ErrorAction SilentlyContinue
    if ($task) {
        Disable-ScheduledTask -TaskName "LifeLogging-Live" | Out-Null
        Write-Host "OK   disabled scheduled task: LifeLogging-Live"
    } else {
        Write-Host "     no LifeLogging-Live task (fine)"
    }
}

# 3) Desktop 任务改用 pythonw.exe：push_desktop.py 不需要控制台
$pyw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$desk = Get-ScheduledTask -TaskName "LifeLogging-Desktop" -ErrorAction SilentlyContinue
if ($desk -and (Test-Path $pyw)) {
    $action = New-ScheduledTaskAction `
        -Execute $pyw `
        -Argument "`"$Root\scripts\push_desktop.py`"" `
        -WorkingDirectory $Root
    Set-ScheduledTask -TaskName "LifeLogging-Desktop" -Action $action | Out-Null
    Write-Host "OK   LifeLogging-Desktop now uses pythonw.exe"
} elseif (-not (Test-Path $pyw)) {
    Write-Host "WARN venv pythonw.exe not found; Desktop task left unchanged" -ForegroundColor Yellow
}

# 4) 立即启动（已在跑的话 Mutex 会让第二个实例自己退出）
if (-not $NoStart) {
    $running = Get-Process -Name "LifeLoggingLive" -ErrorAction SilentlyContinue
    if ($running) {
        Write-Host "     daemon already running (pid $($running[0].Id))"
    } else {
        Start-Process -FilePath $exe -ArgumentList "`"$Root`"" -WorkingDirectory $Root -WindowStyle Hidden
        Write-Host "OK   daemon started (hidden)"
    }
}

Write-Host ""
Write-Host "Logs:"
Write-Host "  state\logs\live-daemon.log   (daemon start / exit / errors)"
Write-Host "  state\logs\live_<date>.log   (each run_live.ps1 round)"
Write-Host ""
Write-Host "Uninstall:"
Write-Host "  Get-Process LifeLoggingLive -ErrorAction SilentlyContinue | Stop-Process"
Write-Host "  Remove-Item `"$lnkPath`" -Force"
Write-Host "  Enable-ScheduledTask -TaskName LifeLogging-Live"
