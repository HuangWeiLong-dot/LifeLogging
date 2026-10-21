# LifeLoggingLive.exe —— 无窗口的实时发布守护程序

**它解决什么。** `LifeLogging-Live` 计划任务每 2 分钟拉起
`powershell.exe ... run_live.ps1`。即使加了 `-WindowStyle Hidden`，任务启动时仍会
闪一个控制台窗口；在无边框全屏游戏里，这个窗口会把鼠标焦点抢走。

这个 WinExe（`/target:winexe`，没有控制台）常驻在用户会话里，带一个托盘图标
和一个简单的控制面板，每 2 分钟用 `CreateNoWindow + Hidden` 的隐藏子进程调用
**同一个** `scripts/run_live.ps1`。脚本逻辑一行没改，变的只是“谁来拉起它、
有没有窗口”。

## 安装

在仓库根：

```powershell
powershell -ExecutionPolicy Bypass -File windows\live-daemon\build.ps1
powershell -ExecutionPolicy Bypass -File windows\live-daemon\install.ps1
```

`install.ps1` 会：

1. 停掉旧实例并**重新编译** `LifeLoggingLive.exe`（Windows 自带 .NET Framework 的 `csc`，不装 SDK）；
2. 在 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` 放
   `LifeLogging Live.lnk` → **登录后自动启动**（无窗口）；
3. **停用** `LifeLogging-Live` 计划任务（避免两份同时跑，也去掉 2 分钟一次的闪窗）；
4. 把 `LifeLogging-Desktop` 任务改成 `pythonw.exe` 跑 `push_desktop.py`
   （另一个每 10 分钟的闪窗源）；
5. 立刻启动新版并**显示一次控制面板**；之后登录启动走 `-silent`，只在托盘。

不需要管理员。

### 为什么是“登录自启”而不是“开机服务”

脚本依赖当前用户的桌面会话：ActivityWatch 的窗口切片、`push_desktop.py` 的
本机快照都在登录会话里。做成 Windows 服务（Session 0）会看不到这些数据。
登录后启动、锁屏时继续跑，是正确的形态。

## UI

- 双击托盘图标（或再次双击 exe）打开控制面板；单实例会把已有窗口叫出来。
- 面板：LIVE / RUNNING NOW / PAUSED、上次运行时间与退出码、下次运行倒计时、
  RUN NOW / PAUSE / OPEN LOGS / OPEN SITE。
- 关闭窗口 = 收进托盘；托盘菜单 EXIT 才真正退出。
- 登录自启带 `-silent`，不会弹窗；托盘图标在系统托盘溢出区。
- 应用图标是 `app.ico`（黑底圆角方块 + 白色脉冲，16–256 多尺寸）：
  `make-icon.py` 生成，`build.ps1` 用 `/win32icon` 嵌进 exe；窗口标题栏、任务栏和
  托盘都读这个 exe 图标。改颜色/形状后重跑 `python windows/live-daemon/make-icon.py`，
  再 `install.ps1`。

## 运行方式

- 单实例：同名 Mutex；不会出现两个守护进程。
- 启动后立刻跑一轮，之后每一轮结束后再等 2 分钟。
- 每轮最多等 8 分钟；超时会杀掉 PowerShell 子进程并记日志，不会把守护进程卡死。
- 完全不显示窗口 / 托盘图标；停止它用任务管理器或下面的命令。

## 日志

```
state\logs\live-daemon.log   启动 / 每轮退出码与耗时 / 超时 / 异常
state\logs\live_<date>.log   run_live.ps1 自己的每轮明细
```

## 卸载 / 回退

```powershell
Get-Process LifeLoggingLive -ErrorAction SilentlyContinue | Stop-Process
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\LifeLogging Live.lnk" -Force
Enable-ScheduledTask -TaskName LifeLogging-Live
```

回退后又会回到每 2 分钟闪一次 PowerShell 的状态。Desktop 任务想恢复成
`python.exe` 的话，重跑 `scripts/setup_scheduler.ps1`（或手动改 action）。

## 注意

- `LifeLogging-Refresh`（每 2 小时）和 `LifeLogging-Daily`（23:00）仍然走
  `powershell.exe -WindowStyle Hidden`；它们不频繁，暂时没改。如果你在这两个时段
  也看到闪窗，可以再包一个 `wscript` 隐藏启动器。
- 本守护程序只在 Windows 用户登录时运行。笔记本关机 / 注销后，站点 NOW 的
  24 小时部分由服务器上的 `lifelogging-live-now.timer` 接管（见
  `server/live-now/README.md`）。
- exe 是构建产物，不进 git（`.gitignore` 里已排除）；源码在
  `windows/live-daemon/Program.cs`。
