// LifeLogging — 全任务调度器 + 看板（无窗口 WinExe）
//
// 为什么需要它：
//   以前 Windows 计划任务会在每个触发点拉起 powershell.exe / python.exe：
//   Live 每 2 分钟、Desktop 每 10 分钟、Refresh 每 2 小时、Daily 每天 23:00
//   以及每次登录 / 解锁。即使加 -WindowStyle Hidden，控制台窗口仍会闪一下；
//   在无边框全屏游戏里还会把鼠标焦点抢走。
//
//   这个 WinExe 没有控制台。它自己常驻在用户会话里，用
//   CreateNoWindow + Hidden 的子进程调用**同一批**脚本，逻辑一行没改。
//   计划任务全部停用（install.ps1 负责），从此不再有窗口闪出来。
//
// 它调度的四件事：
//   LIVE     每 2 分钟      run_live.ps1 -HeavyMinutes 15
//   DESKTOP  每 10 分钟     push_desktop.py
//   REFRESH  每 2 小时      run_daily.ps1 -Refresh（只拉今天，不调 AI）
//   DAILY    每天 23:00     run_daily.ps1（正式处理 + 推进水位线）
//            + 登录 / 解锁后补跑（合盖睡一晚、半夜关机都靠它接住）
//
// UI（看板）：
//   · 四行任务状态：正在跑 / 上次结果 / 下次倒计时 / 单独 RUN
//   · 数据信息：今天时间轴、水位线、最近日记、上次 Daily 结果
//   · 底部实时日志（state\logs\live-daemon.log）
//   · 登录自启带 -silent → 只在托盘；双击托盘或再次双击 exe 打开看板
//
// 行为：
//   · 单实例（Mutex）；同一脚本不会并发跑 —— REFRESH 和 DAILY 共用一把门锁
//   · 每个任务独立线程，LIVE 不会被长时间跑的 DAILY 卡住
//   · 每个任务有超时；退出时杀掉全部子进程
//   · 自己的日志：state\logs\live-daemon.log（>256 KB 截半）
//
// 编译 / 安装见同目录 README.md。

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

namespace LifeLoggingLive
{
    // 一个被调度的任务。字段读写都在 Sync 锁下。
    internal sealed class Job
    {
        public string Key = "";
        public string Name = "";
        public string Desc = "";
        public string Exe = "";
        public string Args = "";

        // 二选一：固定间隔，或每天固定时刻（DailyHour >= 0）。
        public TimeSpan Interval = TimeSpan.Zero;
        public int DailyHour = -1;
        public int DailyMinute;

        public bool RunOnStart;
        public TimeSpan StartDelay = TimeSpan.Zero;
        public TimeSpan Timeout = TimeSpan.FromMinutes(8);

        // 非 null 时，同一把门锁上的任务串行执行（REFRESH 和 DAILY 都是 run_daily.ps1）。
        public object Gate;

        public readonly object Sync = new object();
        public bool Running;
        public DateTime? LastRunAt;
        public DateTime? LastEndAt;
        public DateTime? NextRunAt;
        public int? LastExitCode;
        public double LastSeconds;
        public int RunCount;
    }

    internal static class Program
    {
        private const string MutexName = @"Local\LifeLoggingLiveDaemon";
        private const string ShowEventName = @"Local\LifeLoggingLiveShow";
        private const string SiteUrl = "https://liveinpassion.me/life";

        private static readonly object sync = new object();      // 子进程表 + 全局状态
        private static readonly object dailyGate = new object(); // run_daily.ps1 串行
        private static readonly Dictionary<string, Job> jobsByKey = new Dictionary<string, Job>();
        private static readonly List<Job> jobs = new List<Job>();
        private static readonly List<Process> children = new List<Process>();

        private static string root = "";
        private static string logsDir = "";
        private static string logPath = "";
        private static string pythonExe = "python";

        private static volatile bool paused;
        private static volatile bool exiting;

        private static MainForm form;

        [STAThread]
        private static void Main(string[] args)
        {
            bool created;
            using (var mutex = new Mutex(true, MutexName, out created))
            {
                if (!created)
                {
                    // 已有实例：把它从托盘叫出来，然后自己退出。
                    try
                    {
                        using (var show = EventWaitHandle.OpenExisting(ShowEventName))
                        {
                            show.Set();
                        }
                    }
                    catch
                    {
                        // 老版本没有 show 事件；忽略。
                    }
                    return;
                }

                bool silent = false;
                string rootArg = null;
                for (int i = 0; i < args.Length; i++)
                {
                    if (string.Equals(args[i], "-silent", StringComparison.OrdinalIgnoreCase))
                    {
                        silent = true;
                    }
                    else if (!args[i].StartsWith("-") && rootArg == null)
                    {
                        rootArg = args[i];
                    }
                }

                root = string.IsNullOrEmpty(rootArg)
                    ? AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                    : rootArg;
                try
                {
                    root = Path.GetFullPath(root);
                }
                catch
                {
                    return;
                }

                logsDir = Path.Combine(root, "state", "logs");
                logPath = Path.Combine(logsDir, "live-daemon.log");
                pythonExe = ResolvePython();

                string liveScript = Path.Combine(root, "scripts", "run_live.ps1");
                if (!File.Exists(liveScript))
                {
                    MessageBox.Show("run_live.ps1 not found:\n" + liveScript, "LifeLogging",
                        MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return;
                }

                try
                {
                    Directory.CreateDirectory(logsDir);
                }
                catch
                {
                    // 日志写不了也要继续；live_*.log 还在。
                }

                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.ThreadException += delegate(object s, ThreadExceptionEventArgs e)
                {
                    Log("UI ERROR " + e.Exception.GetType().Name + ": " + e.Exception.Message);
                };
                AppDomain.CurrentDomain.UnhandledException += delegate(object s, UnhandledExceptionEventArgs e)
                {
                    var ex = e.ExceptionObject as Exception;
                    Log("FATAL " + (ex == null ? "unknown" : ex.GetType().Name + ": " + ex.Message));
                };

                BuildJobs();

                // 解锁是「合盖睡一晚」之后唯一的补跑触发点。登录由 RunOnStart 接住。
                try
                {
                    SystemEvents.SessionSwitch += delegate(object s, SessionSwitchEventArgs e)
                    {
                        if (e.Reason == SessionSwitchReason.SessionUnlock)
                        {
                            Log("session unlocked; catch-up scheduled");
                            RequestRunByKey("daily", TimeSpan.FromSeconds(30));
                            RequestRunByKey("desktop", TimeSpan.FromSeconds(5));
                        }
                    };
                }
                catch (Exception ex)
                {
                    Log("WARN could not hook SessionSwitch: " + ex.Message);
                }

                form = new MainForm(silent);
                IntPtr handle = form.Handle; // 先建句柄，show 事件线程才能 BeginInvoke
                // 登录自启的快捷方式是「最小化启动」（WindowStyle=7）。静默实例
                // 如果只建句柄不 Show，窗口会停在 (-32000,-32000) 这个最小化坐标上，
                // 之后 ShowFromTray 只把 WindowState 改回 Normal，位置还是屏幕外。
                // 先强制隐藏，打开面板时再按当前屏幕重新定位。
                form.Hide();
                if (!silent)
                {
                    // ⚠️ 不能在消息循环启动前 form.Show()：实测那样窗口不会
                    //    出现在的 MainWindowHandle 里（安装后「启动成功但看不到
                    //    看板」）。等 Application.Run 起来后再从消息队列显示。
                    form.BeginInvoke(new Action(form.ShowFromTray));
                }

                var showThread = new Thread(ShowLoop);
                showThread.IsBackground = true;
                showThread.Start();

                for (int i = 0; i < jobs.Count; i++)
                {
                    var worker = new Thread(JobLoop);
                    worker.IsBackground = true;
                    worker.Start(jobs[i]);
                }

                Log("started; root=" + root + "; silent=" + silent + "; jobs=" + jobs.Count);
                Application.Run();

                exiting = true;
                KillChildren();
                Log("stopped");
            }
        }

        private static string ResolvePython()
        {
            string venv = Path.Combine(root, ".venv", "Scripts", "python.exe");
            if (File.Exists(venv))
            {
                return venv;
            }
            return "python";
        }

        private static string Quote(string path)
        {
            return "\"" + path + "\"";
        }

        private static void BuildJobs()
        {
            const string ps = "powershell.exe";
            string runLive = Path.Combine(root, "scripts", "run_live.ps1");
            string runDaily = Path.Combine(root, "scripts", "run_daily.ps1");
            string pushDesktop = Path.Combine(root, "scripts", "push_desktop.py");
            string prefix = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File ";

            Add(new Job
            {
                Key = "live",
                Name = "LIVE",
                Desc = "每 2 分钟 · run_live.ps1 -HeavyMinutes 15",
                Exe = ps,
                Args = prefix + Quote(runLive) + " -HeavyMinutes 15",
                Interval = TimeSpan.FromMinutes(2),
                RunOnStart = true,
                StartDelay = TimeSpan.FromSeconds(2),
                Timeout = TimeSpan.FromMinutes(8),
            });

            Add(new Job
            {
                Key = "desktop",
                Name = "DESKTOP",
                Desc = "每 10 分钟 · push_desktop.py（电脑活动快照）",
                Exe = pythonExe,
                Args = Quote(pushDesktop),
                Interval = TimeSpan.FromMinutes(10),
                RunOnStart = true,
                StartDelay = TimeSpan.FromSeconds(20),
                Timeout = TimeSpan.FromMinutes(5),
            });

            Add(new Job
            {
                Key = "refresh",
                Name = "REFRESH",
                Desc = "每 2 小时 · run_daily.ps1 -Refresh（只拉今天，不调 AI）",
                Exe = ps,
                Args = prefix + Quote(runDaily) + " -Refresh",
                Interval = TimeSpan.FromHours(2),
                RunOnStart = false,
                Timeout = TimeSpan.FromMinutes(90),
                Gate = dailyGate,
            });

            Add(new Job
            {
                Key = "daily",
                Name = "DAILY",
                Desc = "每天 23:00 + 登录 / 解锁补跑 · run_daily.ps1（正式处理 + 水位线）",
                Exe = ps,
                Args = prefix + Quote(runDaily),
                DailyHour = 23,
                DailyMinute = 0,
                RunOnStart = true,
                StartDelay = TimeSpan.FromSeconds(45),
                Timeout = TimeSpan.FromMinutes(120),
                Gate = dailyGate,
            });
        }

        private static void Add(Job job)
        {
            jobs.Add(job);
            jobsByKey[job.Key] = job;
        }

        private static void ShowLoop()
        {
            try
            {
                using (var showEvent = new EventWaitHandle(false, EventResetMode.AutoReset, ShowEventName))
                {
                    while (!exiting)
                    {
                        showEvent.WaitOne();
                        if (exiting)
                        {
                            return;
                        }
                        try
                        {
                            if (form != null && form.IsHandleCreated)
                            {
                                form.BeginInvoke(new Action(form.ShowFromTray));
                            }
                        }
                        catch
                        {
                            // 窗口正在销毁；忽略这一次。
                        }
                    }
                }
            }
            catch
            {
                // 无法创建 show 事件（权限/命名冲突）；UI 仍可从托盘打开。
            }
        }

        private static DateTime? ComputeNext(Job job, DateTime now)
        {
            if (job.Interval > TimeSpan.Zero)
            {
                return now.Add(job.Interval);
            }
            if (job.DailyHour >= 0)
            {
                DateTime t = now.Date.AddHours(job.DailyHour).AddMinutes(job.DailyMinute);
                if (t <= now)
                {
                    t = t.AddDays(1);
                }
                return t;
            }
            return null;
        }

        private static void JobLoop(object state)
        {
            var job = (Job)state;

            lock (job.Sync)
            {
                job.NextRunAt = job.RunOnStart
                    ? DateTime.Now.Add(job.StartDelay)
                    : ComputeNext(job, DateTime.Now);
            }

            while (!exiting)
            {
                if (paused)
                {
                    Thread.Sleep(300);
                    continue;
                }

                DateTime? due;
                lock (job.Sync)
                {
                    due = job.NextRunAt;
                }

                if (!due.HasValue)
                {
                    Thread.Sleep(1000);
                    continue;
                }
                if (due.Value > DateTime.Now)
                {
                    Thread.Sleep(400);
                    continue;
                }

                RunJob(job);

                lock (job.Sync)
                {
                    job.NextRunAt = ComputeNext(job, DateTime.Now);
                }
            }
        }

        private static void RunJob(Job job)
        {
            bool gateTaken = false;
            bool started = false;
            bool timedOut = false;
            int exitCode = -1;
            var sw = Stopwatch.StartNew();

            try
            {
                // REFRESH 和 DAILY 都跑 run_daily.ps1：必须串行，否则水位线和
                // SQLite 写入会互相踩。等待门锁的这段时间不算「在跑」。
                if (job.Gate != null)
                {
                    Monitor.Enter(job.Gate);
                    gateTaken = true;
                }

                lock (job.Sync)
                {
                    if (job.Running)
                    {
                        return;
                    }
                    job.Running = true;
                    job.LastRunAt = DateTime.Now;
                    job.LastExitCode = null;
                    started = true;
                }

                var psi = new ProcessStartInfo
                {
                    FileName = job.Exe,
                    Arguments = job.Args,
                    WorkingDirectory = root,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden,
                };

                using (var process = Process.Start(psi))
                {
                    if (process == null)
                    {
                        Log("FAIL " + job.Key + " could not start " + job.Exe);
                    }
                    else
                    {
                        lock (sync)
                        {
                            children.Add(process);
                        }
                        try
                        {
                            if (!process.WaitForExit((int)job.Timeout.TotalMilliseconds))
                            {
                                try { process.Kill(); } catch { }
                                timedOut = true;
                            }
                            else
                            {
                                exitCode = process.ExitCode;
                            }
                        }
                        finally
                        {
                            lock (sync)
                            {
                                children.Remove(process);
                            }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Log("ERROR " + job.Key + " " + ex.GetType().Name + ": " + ex.Message);
            }
            finally
            {
                if (gateTaken)
                {
                    Monitor.Exit(job.Gate);
                }
                sw.Stop();
                if (started)
                {
                    lock (job.Sync)
                    {
                        job.Running = false;
                        job.LastSeconds = sw.Elapsed.TotalSeconds;
                        job.LastExitCode = timedOut ? -1 : exitCode;
                        job.LastEndAt = DateTime.Now;
                        job.RunCount++;
                    }
                    Log(job.Key + (timedOut ? " TIMEOUT " : " exit=") + (timedOut ? -1 : exitCode)
                        + " " + sw.Elapsed.TotalSeconds.ToString("0.0") + "s");
                }
            }
        }

        private static void KillChildren()
        {
            List<Process> snapshot;
            lock (sync)
            {
                snapshot = new List<Process>(children);
                children.Clear();
            }
            for (int i = 0; i < snapshot.Count; i++)
            {
                try { snapshot[i].Kill(); } catch { }
            }
        }

        private static bool RequestRun(Job job, TimeSpan delay)
        {
            if (job == null || exiting)
            {
                return false;
            }
            lock (job.Sync)
            {
                if (job.Running)
                {
                    return false;
                }
                DateTime at = DateTime.Now.Add(delay);
                if (!job.NextRunAt.HasValue || job.NextRunAt.Value > at)
                {
                    job.NextRunAt = at;
                }
            }
            return true;
        }

        internal static bool RequestRunByKey(string key, TimeSpan delay)
        {
            Job job;
            if (!jobsByKey.TryGetValue(key, out job))
            {
                return false;
            }
            return RequestRun(job, delay);
        }

        internal static void TogglePause()
        {
            if (exiting)
            {
                return;
            }
            paused = !paused;
            Log(paused ? "paused" : "resumed");
        }

        internal static bool IsPaused()
        {
            return paused;
        }

        internal static void RequestExit()
        {
            exiting = true;
            KillChildren();
            try
            {
                if (form != null)
                {
                    form.PrepareExit();
                }
            }
            catch { }
            Application.Exit();
        }

        internal static IList<Job> Jobs
        {
            get { return jobs; }
        }

        internal static void OpenLogs()
        {
            try
            {
                Directory.CreateDirectory(logsDir);
                Process.Start("explorer.exe", "\"" + logsDir + "\"");
            }
            catch (Exception ex)
            {
                MessageBox.Show("Could not open logs:\n" + ex.Message, "LifeLogging");
            }
        }

        internal static void OpenVault()
        {
            try
            {
                string vault = Path.Combine(root, "vault");
                if (!Directory.Exists(vault))
                {
                    vault = root;
                }
                Process.Start("explorer.exe", "\"" + vault + "\"");
            }
            catch (Exception ex)
            {
                MessageBox.Show("Could not open the vault:\n" + ex.Message, "LifeLogging");
            }
        }

        internal static void OpenSite()
        {
            try
            {
                Process.Start(SiteUrl);
            }
            catch (Exception ex)
            {
                MessageBox.Show("Could not open the site:\n" + ex.Message, "LifeLogging");
            }
        }

        // ── 看板用的只读数据 ────────────────────────────────────────

        internal static string ReadText(string path)
        {
            try
            {
                if (!File.Exists(path))
                {
                    return "";
                }
                using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                using (var sr = new StreamReader(fs, Encoding.UTF8, true))
                {
                    return sr.ReadToEnd();
                }
            }
            catch
            {
                return "";
            }
        }

        internal static string ReadLogTail(int maxChars)
        {
            string text = ReadText(logPath);
            if (text.Length > maxChars)
            {
                return text.Substring(text.Length - maxChars);
            }
            return text;
        }

        /// 看板顶部：今天时间轴 / 水位线 / 最近日记。
        internal static string InfoLine()
        {
            string today = DateTime.Now.ToString("yyyy-MM-dd");
            string hours = "—";
            string tl = ReadText(Path.Combine(root, "state", "timeline-" + today + ".json"));
            Match mh = Regex.Match(tl, "\"total_hours\"\\s*:\\s*([0-9.]+)");
            if (mh.Success)
            {
                hours = mh.Groups[1].Value + "h";
            }

            string wm = "—";
            string lp = ReadText(Path.Combine(root, "state", "last_processed.json"));
            Match mw = Regex.Match(lp, "\"last_date\"\\s*:\\s*\"([0-9-]+)\"");
            if (mw.Success)
            {
                wm = mw.Groups[1].Value;
            }

            string journal = "—";
            try
            {
                string dir = Path.Combine(root, "vault", "05-Journal");
                if (Directory.Exists(dir))
                {
                    string[] files = Directory.GetFiles(dir, "*.md");
                    string best = null;
                    for (int i = 0; i < files.Length; i++)
                    {
                        string name = Path.GetFileNameWithoutExtension(files[i]);
                        if (!Regex.IsMatch(name, "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"))
                        {
                            continue;
                        }
                        if (best == null || string.CompareOrdinal(name, best) > 0)
                        {
                            best = name;
                        }
                    }
                    if (best != null)
                    {
                        journal = best;
                    }
                }
            }
            catch { }

            return "TODAY  " + hours + "   ·   AI " + TodayCost() + "   ·   水位线  " + wm
                + "   ·   最近日记  " + journal;
        }

        /// 当天已花的 LLM 成本（state\cost_ledger.jsonl 里 day == 今天 的那些行）。
        private static string TodayCost()
        {
            string today = DateTime.Now.ToString("yyyy-MM-dd");
            string text = ReadText(Path.Combine(root, "state", "cost_ledger.jsonl"));
            if (text.Length == 0)
            {
                return "—";
            }
            double sum = 0;
            string[] lines = text.Split('\n');
            for (int i = 0; i < lines.Length; i++)
            {
                string line = lines[i];
                if (line.IndexOf("\"" + today + "\"", StringComparison.Ordinal) < 0)
                {
                    continue;
                }
                Match m = Regex.Match(line, "\"cost\"\\s*:\\s*([0-9.]+)");
                if (m.Success)
                {
                    double v;
                    if (double.TryParse(m.Groups[1].Value,
                        System.Globalization.NumberStyles.Float,
                        System.Globalization.CultureInfo.InvariantCulture, out v))
                    {
                        sum += v;
                    }
                }
            }
            return sum.ToString("0.00") + " 元";
        }

        /// 上次 run_daily 的结论（state\last_run.md 的第一张表）。
        internal static string LastDailyLine()
        {
            string md = ReadText(Path.Combine(root, "state", "last_run.md"));
            if (md.Length == 0)
            {
                return "LAST DAILY  —";
            }
            string time = Match1(md, "\\|\\s*时间\\s*\\|\\s*([^|]+?)\\s*\\|");
            string result = Match1(md, "\\|\\s*结果\\s*\\|\\s*([^|]+?)\\s*\\|");
            string wm = Match1(md, "\\|\\s*水位线\\s*\\|\\s*([^|]+?)\\s*\\|");
            if (time.Length == 0 && result.Length == 0)
            {
                return "LAST DAILY  —";
            }
            return "LAST DAILY  " + time + "   ·   " + result + "   ·   水位线 " + wm;
        }

        private static string Match1(string text, string pattern)
        {
            Match m = Regex.Match(text, pattern);
            return m.Success ? m.Groups[1].Value.Trim() : "";
        }

        internal static void Log(string message)
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

        // ── 看板窗口 ────────────────────────────────────────────────

        private sealed class JobRow
        {
            public Job Job;
            public Label Name;
            public Label Desc;
            public Label Last;
            public Label Next;
            public Label State;
            public Button Run;
        }

        private sealed class MainForm : Form
        {
            private readonly Size panelSize;
            private readonly Label statusLabel;
            private readonly Label infoLabel;
            private readonly Label dailyLabel;
            private readonly Button pauseButton;
            private readonly TextBox logBox;
            private readonly NotifyIcon tray;
            private readonly ToolStripMenuItem pauseItem;
            private readonly JobRow[] rows;
            private readonly System.Windows.Forms.Timer uiTimer;
            private readonly System.Windows.Forms.Timer infoTimer;
            private readonly System.Windows.Forms.Timer logTimer;
            private string logText = "";

            private readonly Color bg = Color.FromArgb(18, 18, 18);
            private readonly Color card = Color.FromArgb(24, 24, 24);
            private readonly Color fg = Color.FromArgb(236, 236, 236);
            private readonly Color dim = Color.FromArgb(145, 145, 145);
            private readonly Color line = Color.FromArgb(58, 58, 58);
            private readonly Color green = Color.FromArgb(138, 226, 52);
            private readonly Color amber = Color.FromArgb(232, 193, 90);
            private readonly Color cyan = Color.FromArgb(103, 213, 255);
            private readonly Color red = Color.FromArgb(255, 107, 107);

            public MainForm(bool silent)
            {
                Text = "LifeLogging";
                ClientSize = new Size(760, 632);
                FormBorderStyle = FormBorderStyle.FixedSingle;
                MaximizeBox = false;
                StartPosition = FormStartPosition.CenterScreen;
                BackColor = bg;
                ForeColor = fg;
                Font = new Font("Segoe UI", 9f);
                Icon appIcon = null;
                try
                {
                    appIcon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
                }
                catch { }
                if (appIcon == null)
                {
                    appIcon = SystemIcons.Application;
                }
                Icon = appIcon;
                ShowInTaskbar = !silent;

                var title = MakeLabel("LIFELOGGING", 18, 12, 320, 20, 11f, FontStyle.Bold, dim);
                title.Font = new Font("Consolas", 11f, FontStyle.Bold);
                var sub = MakeLabel("ALL JOBS · HIDDEN RUNNER · LOGIN AUTOSTART",
                    18, 32, 560, 16, 8f, FontStyle.Regular, dim);
                sub.Font = new Font("Consolas", 8f);

                statusLabel = MakeLabel("ACTIVE", 560, 8, 176, 32, 15f, FontStyle.Bold, green);
                statusLabel.Font = new Font("Consolas", 15f, FontStyle.Bold);
                statusLabel.TextAlign = ContentAlignment.MiddleRight;

                Controls.Add(title);
                Controls.Add(sub);
                Controls.Add(statusLabel);

                IList<Job> jobs = Program.Jobs;
                rows = new JobRow[jobs.Count];
                for (int i = 0; i < jobs.Count; i++)
                {
                    rows[i] = BuildRow(jobs[i], 58 + i * 66);
                }

                infoLabel = MakeLabel("", 18, 322, 724, 16, 8.5f, FontStyle.Regular, fg);
                infoLabel.Font = new Font("Consolas", 8.5f);
                dailyLabel = MakeLabel("", 18, 340, 724, 16, 8f, FontStyle.Regular, dim);
                dailyLabel.Font = new Font("Consolas", 8f);
                Controls.Add(infoLabel);
                Controls.Add(dailyLabel);

                pauseButton = MakeButton("PAUSE ALL", 18, 362, 112);
                pauseButton.Click += delegate { Program.TogglePause(); };
                var runLive = MakeButton("RUN LIVE", 136, 362, 112);
                runLive.Click += delegate { Program.RequestRunByKey("live", TimeSpan.Zero); };
                var runDaily = MakeButton("RUN DAILY", 254, 362, 112);
                runDaily.Click += delegate { Program.RequestRunByKey("daily", TimeSpan.Zero); };
                var logsButton = MakeButton("OPEN LOGS", 372, 362, 112);
                logsButton.Click += delegate { Program.OpenLogs(); };
                var vaultButton = MakeButton("OPEN VAULT", 490, 362, 112);
                vaultButton.Click += delegate { Program.OpenVault(); };
                var siteButton = MakeButton("OPEN SITE", 608, 362, 112);
                siteButton.Click += delegate { Program.OpenSite(); };
                Controls.Add(pauseButton);
                Controls.Add(runLive);
                Controls.Add(runDaily);
                Controls.Add(logsButton);
                Controls.Add(vaultButton);
                Controls.Add(siteButton);

                logBox = new TextBox();
                logBox.Location = new Point(18, 402);
                logBox.Size = new Size(724, 196);
                logBox.Multiline = true;
                logBox.ReadOnly = true;
                logBox.WordWrap = false;
                logBox.ScrollBars = ScrollBars.Vertical;
                logBox.BackColor = card;
                logBox.ForeColor = dim;
                logBox.BorderStyle = BorderStyle.FixedSingle;
                logBox.Font = new Font("Consolas", 8f);
                Controls.Add(logBox);

                var foot = MakeLabel("计划任务已停用 · 子进程全部 CreateNoWindow · 日志 state\\logs\\live-daemon.log",
                    18, 604, 724, 16, 8f, FontStyle.Regular, dim);
                foot.Font = new Font("Consolas", 8f);
                Controls.Add(foot);

                var menu = new ContextMenuStrip();
                var openItem = new ToolStripMenuItem("Open dashboard");
                openItem.Click += delegate { ShowFromTray(); };
                var liveItem = new ToolStripMenuItem("Run live now");
                liveItem.Click += delegate { Program.RequestRunByKey("live", TimeSpan.Zero); };
                var dailyItem = new ToolStripMenuItem("Run daily now");
                dailyItem.Click += delegate { Program.RequestRunByKey("daily", TimeSpan.Zero); };
                pauseItem = new ToolStripMenuItem("Pause all");
                pauseItem.Click += delegate { Program.TogglePause(); };
                var logsItem = new ToolStripMenuItem("Open logs");
                logsItem.Click += delegate { Program.OpenLogs(); };
                var exitItem = new ToolStripMenuItem("Exit");
                exitItem.Click += delegate { Program.RequestExit(); };
                menu.Items.Add(openItem);
                menu.Items.Add(liveItem);
                menu.Items.Add(dailyItem);
                menu.Items.Add(new ToolStripSeparator());
                menu.Items.Add(pauseItem);
                menu.Items.Add(logsItem);
                menu.Items.Add(new ToolStripSeparator());
                menu.Items.Add(exitItem);

                tray = new NotifyIcon();
                tray.Icon = appIcon;
                tray.Text = "LifeLogging";
                tray.ContextMenuStrip = menu;
                tray.Visible = true;
                tray.DoubleClick += delegate { ShowFromTray(); };

                uiTimer = new System.Windows.Forms.Timer();
                uiTimer.Interval = 1000;
                uiTimer.Tick += delegate { UpdateUi(); };
                uiTimer.Start();

                infoTimer = new System.Windows.Forms.Timer();
                infoTimer.Interval = 5000;
                infoTimer.Tick += delegate { RefreshInfo(); };
                infoTimer.Start();

                logTimer = new System.Windows.Forms.Timer();
                logTimer.Interval = 2000;
                logTimer.Tick += delegate { RefreshLog(); };
                logTimer.Start();

                UpdateUi();
                RefreshInfo();
                RefreshLog();
                panelSize = Size;
            }

            private JobRow BuildRow(Job job, int y)
            {
                var row = new JobRow();
                row.Job = job;

                row.Name = MakeLabel(job.Name, 18, y, 260, 18, 9.5f, FontStyle.Bold, fg);
                row.Name.Font = new Font("Consolas", 9.5f, FontStyle.Bold);
                row.Desc = MakeLabel(job.Desc, 18, y + 19, 560, 16, 8f, FontStyle.Regular, dim);
                row.Desc.Font = new Font("Consolas", 8f);
                row.Last = MakeLabel("last  —", 18, y + 37, 380, 16, 8f, FontStyle.Regular, dim);
                row.Last.Font = new Font("Consolas", 8f);
                row.Next = MakeLabel("next  —", 400, y + 37, 200, 16, 8f, FontStyle.Regular, dim);
                row.Next.Font = new Font("Consolas", 8f);
                row.State = MakeLabel("IDLE", 492, y + 6, 120, 16, 8f, FontStyle.Bold, dim);
                row.State.Font = new Font("Consolas", 8f, FontStyle.Bold);
                row.State.TextAlign = ContentAlignment.MiddleRight;

                row.Run = MakeButton("RUN", 618, y + 12, 118);
                string key = job.Key;
                row.Run.Click += delegate { Program.RequestRunByKey(key, TimeSpan.Zero); };

                var divider = new Panel();
                divider.Location = new Point(18, y + 60);
                divider.Size = new Size(724, 1);
                divider.BackColor = line;

                Controls.Add(row.Name);
                Controls.Add(row.Desc);
                Controls.Add(row.Last);
                Controls.Add(row.Next);
                Controls.Add(row.State);
                Controls.Add(row.Run);
                Controls.Add(divider);
                return row;
            }

            protected override void OnResize(EventArgs e)
            {
                base.OnResize(e);
                if (WindowState == FormWindowState.Minimized)
                {
                    HideToTray();
                }
            }

            protected override void OnFormClosing(FormClosingEventArgs e)
            {
                if (!exiting && e.CloseReason == CloseReason.UserClosing)
                {
                    e.Cancel = true;
                    HideToTray();
                    return;
                }
                base.OnFormClosing(e);
            }

            public void ShowFromTray()
            {
                if (InvokeRequired)
                {
                    BeginInvoke(new Action(ShowFromTray));
                    return;
                }

                // 静默启动的窗口可能还停在最小化坐标 (-32000,-32000)。
                if (WindowState == FormWindowState.Minimized || !IsOnScreen())
                {
                    CenterOnCursorScreen();
                }

                Show();
                ShowInTaskbar = true;

                // 登录快捷方式是 SW_SHOWMINIMIZED；第一次 Show() 时窗口可能
                // 仍带着最小化状态。此时 WinForms 的 WindowState 可能报告
                // Normal，不能再写 WindowState（反而会把窗口真正最小化），
                // 必须用 Win32 的 SW_RESTORE 恢复。
                if (IsIconic(Handle))
                {
                    ShowWindow(Handle, SW_RESTORE);
                }
                if (!IsOnScreen())
                {
                    CenterOnCursorScreen();
                }

                Activate();
                BringToFront();
                UpdateUi();
                RefreshInfo();
                RefreshLog();
            }

            private void CenterOnCursorScreen()
            {
                Rectangle area = Screen.FromPoint(Cursor.Position).WorkingArea;
                StartPosition = FormStartPosition.Manual;
                Size = panelSize;
                Location = new Point(
                    area.Left + Math.Max(0, (area.Width - panelSize.Width) / 2),
                    area.Top + Math.Max(0, (area.Height - panelSize.Height) / 2));
            }

            private bool IsOnScreen()
            {
                Rectangle bounds = Bounds;
                foreach (Screen screen in Screen.AllScreens)
                {
                    if (screen.WorkingArea.IntersectsWith(bounds))
                    {
                        return true;
                    }
                }
                return false;
            }

            [System.Runtime.InteropServices.DllImport("user32.dll")]
            private static extern bool IsIconic(IntPtr hWnd);

            [System.Runtime.InteropServices.DllImport("user32.dll")]
            private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

            private const int SW_RESTORE = 9;

            private void HideToTray()
            {
                Hide();
                ShowInTaskbar = false;
            }

            public void PrepareExit()
            {
                try
                {
                    uiTimer.Stop();
                    infoTimer.Stop();
                    logTimer.Stop();
                    tray.Visible = false;
                    tray.Dispose();
                }
                catch { }
            }

            private Label MakeLabel(string text, int x, int y, int w, int h,
                float size, FontStyle style, Color color)
            {
                var l = new Label();
                l.Text = text;
                l.Location = new Point(x, y);
                l.Size = new Size(w, h);
                l.AutoSize = false;
                l.Font = new Font("Segoe UI", size, style);
                l.ForeColor = color;
                l.BackColor = Color.Transparent;
                return l;
            }

            private Button MakeButton(string text, int x, int y, int w)
            {
                var b = new Button();
                b.Text = text;
                b.Location = new Point(x, y);
                b.Size = new Size(w, 28);
                b.FlatStyle = FlatStyle.Flat;
                b.FlatAppearance.BorderSize = 1;
                b.FlatAppearance.BorderColor = line;
                b.FlatAppearance.MouseOverBackColor = Color.FromArgb(42, 42, 42);
                b.FlatAppearance.MouseDownBackColor = Color.FromArgb(55, 55, 55);
                b.BackColor = Color.FromArgb(28, 28, 28);
                b.ForeColor = fg;
                b.Font = new Font("Consolas", 8.5f, FontStyle.Regular);
                b.Cursor = Cursors.Hand;
                b.UseVisualStyleBackColor = false;
                return b;
            }

            private static string Countdown(TimeSpan left)
            {
                if (left.TotalSeconds < 0)
                {
                    left = TimeSpan.Zero;
                }
                if (left.TotalSeconds < 60)
                {
                    return ((int)left.TotalSeconds) + "s";
                }
                if (left.TotalMinutes < 60)
                {
                    return ((int)left.TotalMinutes) + "m";
                }
                return string.Format("{0}h{1:00}m", (int)left.TotalHours, left.Minutes);
            }

            private void RefreshInfo()
            {
                infoLabel.Text = Program.InfoLine();
                dailyLabel.Text = Program.LastDailyLine();
            }

            private void RefreshLog()
            {
                if (!Visible)
                {
                    return;
                }
                string t = Program.ReadLogTail(12000);
                if (t == logText)
                {
                    return;
                }
                logText = t;
                logBox.Text = t;
                logBox.SelectionStart = logBox.TextLength;
                logBox.ScrollToCaret();
            }

            private void UpdateUi()
            {
                bool p = Program.IsPaused();
                bool exitNow = exiting;
                bool anyRunning = false;
                int active = 0;

                for (int i = 0; i < rows.Length; i++)
                {
                    Job job = rows[i].Job;
                    bool running;
                    DateTime? last;
                    DateTime? end;
                    DateTime? next;
                    int? code;
                    double secs;
                    int count;
                    lock (job.Sync)
                    {
                        running = job.Running;
                        last = job.LastRunAt;
                        end = job.LastEndAt;
                        next = job.NextRunAt;
                        code = job.LastExitCode;
                        secs = job.LastSeconds;
                        count = job.RunCount;
                    }
                    if (running)
                    {
                        anyRunning = true;
                        active++;
                    }

                    string state;
                    Color sc;
                    if (running)
                    {
                        state = "RUNNING";
                        sc = cyan;
                    }
                    else if (p)
                    {
                        state = "PAUSED";
                        sc = amber;
                    }
                    else if (code.HasValue && code.Value != 0)
                    {
                        state = "ERROR";
                        sc = red;
                    }
                    else if (count == 0)
                    {
                        state = "WAITING";
                        sc = dim;
                    }
                    else
                    {
                        state = "IDLE";
                        sc = green;
                    }
                    rows[i].State.Text = state;
                    rows[i].State.ForeColor = sc;

                    if (running && last.HasValue)
                    {
                        rows[i].Last.Text = "running since " + last.Value.ToString("HH:mm:ss") + " …";
                        rows[i].Last.ForeColor = cyan;
                    }
                    else if (end.HasValue)
                    {
                        string codeText = code.HasValue ? code.Value.ToString() : "—";
                        rows[i].Last.Text = string.Format("last {0}  ·  exit {1}  ·  {2:0.0}s  ·  {3} runs",
                            end.Value.ToString("HH:mm:ss"), codeText, secs, count);
                        rows[i].Last.ForeColor = (code.HasValue && code.Value == 0) ? fg : red;
                    }
                    else
                    {
                        rows[i].Last.Text = "last  —";
                        rows[i].Last.ForeColor = dim;
                    }

                    if (running)
                    {
                        rows[i].Next.Text = "next  after this run";
                        rows[i].Next.ForeColor = dim;
                    }
                    else if (p)
                    {
                        rows[i].Next.Text = "next  paused";
                        rows[i].Next.ForeColor = amber;
                    }
                    else if (next.HasValue)
                    {
                        TimeSpan left = next.Value - DateTime.Now;
                        if (left.TotalSeconds < 0)
                        {
                            left = TimeSpan.Zero;
                        }
                        rows[i].Next.Text = string.Format("next {0}  ·  in {1}",
                            next.Value.ToString("HH:mm"), Countdown(left));
                        rows[i].Next.ForeColor = dim;
                    }
                    else
                    {
                        rows[i].Next.Text = "next  —";
                        rows[i].Next.ForeColor = dim;
                    }

                    rows[i].Run.Enabled = !p && !running && !exitNow;
                }

                string global;
                Color gc;
                if (exitNow)
                {
                    global = "STOPPING";
                    gc = dim;
                }
                else if (p)
                {
                    global = "PAUSED";
                    gc = amber;
                }
                else if (anyRunning)
                {
                    global = "BUSY";
                    gc = cyan;
                }
                else
                {
                    global = "ACTIVE";
                    gc = green;
                }
                statusLabel.Text = global;
                statusLabel.ForeColor = gc;

                pauseButton.Text = p ? "RESUME ALL" : "PAUSE ALL";
                pauseButton.Enabled = !exitNow;
                pauseItem.Text = p ? "Resume all" : "Pause all";

                string tip = "LifeLogging · " + global;
                if (anyRunning)
                {
                    tip += " (" + active + " running)";
                }
                if (tip.Length > 63)
                {
                    tip = tip.Substring(0, 63);
                }
                tray.Text = tip;
            }
        }
    }
}
