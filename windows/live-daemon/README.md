# LifeLoggingLive.exe —— 全任务调度器 + 看板（无窗口）

**它解决什么。** 以前 Windows 计划任务会在每个触发点拉起 `powershell.exe` /
`python.exe`：Live 每 2 分钟、Desktop 每 10 分钟、Refresh 每 2 小时、Daily
每天 23:00 以及每次登录 / 解锁。即使加了 `-WindowStyle Hidden`，控制台窗口仍会
闪一下；在无边框全屏游戏里还会把鼠标焦点抢走。

这个 WinExe（`/target:winexe`，没有控制台）常驻在用户会话里，用
`CreateNoWindow + Hidden` 的隐藏子进程调用**同一批脚本**——脚本逻辑一行没改，
变的只是「谁来拉起它、有没有窗口」。安装后**四个计划任务全部停用**。

## 它调度什么

| 任务 | 节奏 | 跑什么 |
|---|---|---|
| `LIVE` | 每 2 分钟 | `scripts\run_live.ps1 -HeavyMinutes 15` |
| `DESKTOP` | 每 10 分钟 | `scripts\push_desktop.py` |
| `REFRESH` | 每 2 小时 | `scripts\run_daily.ps1 -Refresh`（只拉今天，不调 AI） |
| `DAILY` | 每天 23:00 + 登录 / 解锁补跑 | `scripts\run_daily.ps1`（正式处理 + 推进水位线） |

`REFRESH` 和 `DAILY` 都跑 `run_daily.ps1`，共享一把门锁，**不会同时跑**。
`LIVE` / `DESKTOP` 各自独立线程——`DAILY` 跑 20 分钟也不会把实时发布卡住。

登录 / 解锁补跑对应原来计划任务的两个触发器：合盖睡一晚 → 解锁时补；
关机 / 重启 → 登录时补。`run_daily.ps1` 幂等、无事可做会立刻退出。

## 安装

在仓库根：

```powershell
powershell -ExecutionPolicy Bypass -File windows\live-daemon\install.ps1
```

`install.ps1` 会：

1. 停掉旧实例并**重新编译** `LifeLoggingLive.exe`（Windows 自带 .NET Framework
   的 `csc`，不装 SDK）；
2. 在 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` 放
   `LifeLogging Live.lnk` → **登录后自动启动**（`-silent`，只在托盘）；
3. **停用** `LifeLogging-Live` / `-Desktop` / `-Refresh` / `-Daily` 四个计划任务
   （留着不删，方便 `Enable-ScheduledTask` 回退）；
4. 立刻启动新版并**显示一次看板**；之后登录启动走 `-silent`，不弹窗。

不需要管理员。

### 为什么是“登录自启”而不是“开机服务”

脚本依赖当前用户的桌面会话：ActivityWatch 的窗口切片、`push_desktop.py` 的
本机快照都在登录会话里。做成 Windows 服务（Session 0）会看不到这些数据。
登录后启动、锁屏时继续跑，是正确的形态。

## 看板

- 双击托盘图标（或再次双击 exe）打开看板；单实例会把已有窗口叫出来。
- 四行任务：`RUNNING / IDLE / WAITING / PAUSED / ERROR`、上次结束时间与退出码、
  下次运行倒计时、单独 `RUN`。
- 数据行：今天的时间轴小时数、水位线、最近一篇日记、上次 Daily 的结论
  （读 `state\last_run.md`）。
- 底部实时日志：`state\logs\live-daemon.log` 的尾部，每 2 秒刷新。
- 按钮：`PAUSE ALL`（暂停调度，不杀正在跑的子进程）、`RUN LIVE`、`RUN DAILY`、
  `OPEN LOGS`、`OPEN VAULT`、`OPEN SITE`。
- 关闭窗口 = 收进托盘；托盘菜单 `Exit` 才真正退出（退出时会杀掉全部子进程）。

## 运行方式

- 单实例：同名 Mutex；不会出现两个调度器。
- 每个任务独立线程；到点即跑，跑完再排下一次。
- 超时：Live 8 分钟、Desktop 5 分钟、Refresh 90 分钟、Daily 120 分钟。
  超时会杀掉子进程并记 `TIMEOUT`，不会把调度器卡死。
- 完全不显示窗口；托盘图标在系统托盘溢出区。

## 日志

```
state\logs\live-daemon.log   启动 / 每个任务的退出码与耗时 / 超时 / 异常
state\logs\live_<date>.log   run_live.ps1 自己的每轮明细
```

## 卸载 / 回退

```powershell
Get-Process LifeLoggingLive -ErrorAction SilentlyContinue | Stop-Process
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\LifeLogging Live.lnk" -Force
Enable-ScheduledTask -TaskName LifeLogging-Live,LifeLogging-Desktop,LifeLogging-Refresh,LifeLogging-Daily
```

回退后又会回到计划任务闪窗的模式。

## 注意

- 计划任务停用后，**调度器没在跑 = 什么都不会自动跑**。它挂在当前用户会话里，
  登录自启；如果被手动杀掉，重新双击 exe 即可。
- 23:00 那次如果机器在睡眠，调度器唤不醒机器（和原来的计划任务一样，
  `WakeToRun=False`）。下一次登录 / 解锁会补跑，这正是 `run_daily.ps1`
  水位线逻辑的用武之地。
- 应用图标是 `app.ico`（见 `make-icon.py`），`build.ps1` 用 `/win32icon`
  嵌进 exe。
