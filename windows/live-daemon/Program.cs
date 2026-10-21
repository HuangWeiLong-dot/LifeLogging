// LifeLogging Live daemon
//
// 为什么需要它：
//   LifeLogging-Live 计划任务每 2 分钟拉起 powershell.exe 跑 run_live.ps1：
//   控制台窗口会闪一下，在无边框全屏游戏里还会把焦点抢走。这个 WinExe
//   没有控制台（/target:winexe），它自己常驻，用 CreateNoWindow 的隐藏子进程
//   调用同一个 run_live.ps1 —— 计划任务可以关掉，脚本逻辑一行没变。
//
// 行为：
//   · 单实例（同名 Mutex）；已在跑时第二个直接退出
//   · 启动后立刻跑一轮，然后每 2 分钟一轮（上一轮结束后再计时）
//   · 每轮最多等 8 分钟，超时就杀掉子进程，避免卡死整个守护进程
//   · 自己的小日志：state\logs\live-daemon.log（超过 256 KB 截半）
//
// 编译 / 安装见同目录 README.md；安装脚本会把它放进启动文件夹并停用计划任务。

using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

namespace LifeLoggingLive
{
    internal static class Program
    {
        private const string MutexName = @"Local\LifeLoggingLiveDaemon";
        private static readonly TimeSpan Interval = TimeSpan.FromMinutes(2);
        private static readonly TimeSpan RunTimeout = TimeSpan.FromMinutes(8);

        private static string root = "";
        private static string logPath = "";

        [STAThread]
        private static void Main(string[] args)
        {
            bool created;
            using (var mutex = new Mutex(true, MutexName, out created))
            {
                if (!created)
                {
                    return;
                }

                root = args.Length > 0 && !string.IsNullOrWhiteSpace(args[0])
                    ? args[0]
                    : AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                root = Path.GetFullPath(root);
                logPath = Path.Combine(root, "state", "logs", "live-daemon.log");

                var script = Path.Combine(root, "scripts", "run_live.ps1");
                if (!File.Exists(script))
                {
                    Log("FAIL run_live.ps1 not found: " + script);
                    return;
                }

                try
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(logPath));
                }
                catch
                {
                    // 日志写不了也要继续跑；控制台不可见，用户靠 live_*.log 排查。
                }

                Log("started; root=" + root + "; interval=" + Interval.TotalMinutes + "m");

                while (true)
                {
                    RunOnce(script);
                    Thread.Sleep(Interval);
                }
            }
        }

        private static void RunOnce(string script)
        {
            var sw = Stopwatch.StartNew();
            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = "powershell.exe",
                    Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File \""
                        + script + "\" -HeavyMinutes 15",
                    WorkingDirectory = root,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden,
                };

                using (var process = Process.Start(psi))
                {
                    if (process == null)
                    {
                        Log("FAIL could not start powershell");
                        return;
                    }

                    if (!process.WaitForExit((int)RunTimeout.TotalMilliseconds))
                    {
                        try { process.Kill(); } catch { }
                        Log("TIMEOUT after " + RunTimeout.TotalMinutes + "m; killed");
                        return;
                    }

                    sw.Stop();
                    Log("run exit=" + process.ExitCode + " " + sw.Elapsed.TotalSeconds.ToString("0.0") + "s");
                }
            }
            catch (Exception ex)
            {
                sw.Stop();
                Log("ERROR " + ex.GetType().Name + ": " + ex.Message);
            }
        }

        private static void Log(string message)
        {
            try
            {
                if (string.IsNullOrEmpty(logPath))
                {
                    return;
                }
                var line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message + Environment.NewLine;
                File.AppendAllText(logPath, line);

                var info = new FileInfo(logPath);
                if (info.Length > 256 * 1024)
                {
                    var text = File.ReadAllText(logPath);
                    File.WriteAllText(logPath, text.Substring(text.Length / 2));
                }
            }
            catch
            {
                // 日志失败不能影响主循环。
            }
        }
    }
}
