// LifeLogging Live — 无窗口守护程序 + 简单控制面板
//
// 为什么需要它：
//   LifeLogging-Live 计划任务每 2 分钟拉起 powershell.exe 跑 run_live.ps1：
//   控制台窗口会闪一下，在无边框全屏游戏里还会把焦点抢走。这个 WinExe
//   没有控制台；它自己常驻，用 CreateNoWindow 的隐藏子进程调用同一个
//   run_live.ps1。计划任务可以关掉，脚本逻辑一行没变。
//
// UI：
//   · 登录时由启动文件夹快捷方式带 -silent 启动 → 只在托盘，不弹窗、不抢焦点
//   · 双击托盘图标，或再次双击 exe（单实例会唤醒已有窗口）→ 打开控制面板
//   · 面板：状态 / 上次运行 / 下次运行 / RUN NOW / PAUSE / OPEN LOGS / OPEN SITE
//   · 关掉窗口 = 收进托盘；只有托盘菜单 EXIT 才真正退出
//
// 行为：
//   · 单实例（Mutex）；启动后立刻跑一轮，之后每轮结束再等 2 分钟
//   · 每轮最多等 8 分钟，超时杀掉子进程
//   · 自己的日志：state\logs\live-daemon.log（>256 KB 截半）
//
// 编译 / 安装见同目录 README.md。

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

namespace LifeLoggingLive
{
    internal static class Program
    {
        private const string MutexName = @"Local\LifeLoggingLiveDaemon";
        private const string ShowEventName = @"Local\LifeLoggingLiveShow";
        private const string SiteUrl = "https://liveinpassion.me/life";

        private static readonly TimeSpan Interval = TimeSpan.FromMinutes(2);
        private static readonly TimeSpan RunTimeout = TimeSpan.FromMinutes(8);
        private static readonly object sync = new object();
        private static readonly AutoResetEvent wake = new AutoResetEvent(false);

        private static string root = "";
        private static string script = "";
        private static string logsDir = "";
        private static string logPath = "";

        private static volatile bool paused;
        private static volatile bool exiting;
        private static volatile bool runInProgress;
        private static DateTime? lastRunAt;
        private static DateTime? nextRunAt;
        private static int? lastExitCode;
        private static double lastSeconds;
        private static int runCount;
        private static Process child;

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

                script = Path.Combine(root, "scripts", "run_live.ps1");
                logsDir = Path.Combine(root, "state", "logs");
                logPath = Path.Combine(logsDir, "live-daemon.log");

                if (!File.Exists(script))
                {
                    MessageBox.Show("run_live.ps1 not found:\n" + script, "LifeLogging Live",
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

                form = new MainForm(silent);
                IntPtr handle = form.Handle; // 先建句柄，show 事件线程才能 BeginInvoke
                // 登录自启的快捷方式是「最小化启动」（WindowStyle=7）。静默实例
                // 如果只建句柄不 Show，窗口会停在 (-32000,-32000) 这个最小化坐标上，
                // 之后 ShowFromTray 只把 WindowState 改回 Normal，位置还是屏幕外。
                // 先强制隐藏，打开面板时再按当前屏幕重新定位。
                form.Hide();
                if (!silent)
                {
                    form.Show(); // 手动启动 / 安装后：直接把面板亮出来
                }

                var showThread = new Thread(ShowLoop);
                showThread.IsBackground = true;
                showThread.Start();

                var worker = new Thread(WorkerLoop);
                worker.IsBackground = true;
                worker.Start();

                Log("started; root=" + root + "; silent=" + silent);
                Application.Run();

                exiting = true;
                KillChild();
                Log("stopped");
            }
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

        private static void WorkerLoop()
        {
            while (!exiting)
            {
                bool isPaused;
                lock (sync)
                {
                    isPaused = paused;
                }
                if (isPaused)
                {
                    wake.WaitOne(500);
                    continue;
                }

                RunOnce();

                lock (sync)
                {
                    nextRunAt = DateTime.Now.Add(Interval);
                }
                wake.WaitOne(Interval);
            }
        }

        private static void RunOnce()
        {
            lock (sync)
            {
                runInProgress = true;
                lastRunAt = DateTime.Now;
            }

            var sw = Stopwatch.StartNew();
            int exitCode = -1;
            bool timedOut = false;

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
                    }
                    else
                    {
                        lock (sync)
                        {
                            child = process;
                        }
                        if (!process.WaitForExit((int)RunTimeout.TotalMilliseconds))
                        {
                            try { process.Kill(); } catch { }
                            timedOut = true;
                        }
                        else
                        {
                            exitCode = process.ExitCode;
                        }
                        lock (sync)
                        {
                            child = null;
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Log("ERROR " + ex.GetType().Name + ": " + ex.Message);
            }
            finally
            {
                sw.Stop();
                lock (sync)
                {
                    runInProgress = false;
                    lastSeconds = sw.Elapsed.TotalSeconds;
                    lastExitCode = timedOut ? -1 : exitCode;
                    runCount++;
                }
                Log((timedOut ? "TIMEOUT " : "run ") + "exit=" + (timedOut ? -1 : exitCode)
                    + " " + sw.Elapsed.TotalSeconds.ToString("0.0") + "s");
            }
        }

        private static void KillChild()
        {
            lock (sync)
            {
                if (child != null)
                {
                    try { child.Kill(); } catch { }
                    child = null;
                }
            }
        }

        private static void RequestRun()
        {
            lock (sync)
            {
                if (paused || exiting || runInProgress)
                {
                    return;
                }
            }
            wake.Set();
        }

        private static void TogglePause()
        {
            lock (sync)
            {
                if (exiting)
                {
                    return;
                }
                paused = !paused;
            }
            wake.Set();
        }

        private static void RequestExit()
        {
            exiting = true;
            KillChild();
            wake.Set();
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

        private static void OpenLogs()
        {
            try
            {
                Directory.CreateDirectory(logsDir);
                Process.Start("explorer.exe", "\"" + logsDir + "\"");
            }
            catch (Exception ex)
            {
                MessageBox.Show("Could not open logs:\n" + ex.Message, "LifeLogging Live");
            }
        }

        private static void OpenSite()
        {
            try
            {
                Process.Start(SiteUrl);
            }
            catch (Exception ex)
            {
                MessageBox.Show("Could not open the site:\n" + ex.Message, "LifeLogging Live");
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

        private sealed class MainForm : Form
        {
            private readonly Size panelSize;
            private readonly Label statusLabel;
            private readonly Label lastLabel;
            private readonly Label nextLabel;
            private readonly Label infoLabel;
            private readonly Button runButton;
            private readonly Button pauseButton;
            private readonly NotifyIcon tray;
            private readonly ToolStripMenuItem pauseItem;
            private readonly ToolStripMenuItem runItem;
            private readonly System.Windows.Forms.Timer uiTimer;

            private readonly Color bg = Color.FromArgb(18, 18, 18);
            private readonly Color fg = Color.FromArgb(236, 236, 236);
            private readonly Color dim = Color.FromArgb(145, 145, 145);
            private readonly Color line = Color.FromArgb(58, 58, 58);
            private readonly Color green = Color.FromArgb(138, 226, 52);
            private readonly Color amber = Color.FromArgb(232, 193, 90);
            private readonly Color cyan = Color.FromArgb(103, 213, 255);
            private readonly Color red = Color.FromArgb(255, 107, 107);

            public MainForm(bool silent)
            {
                Text = "LifeLogging Live";
                ClientSize = new Size(424, 244);
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

                var title = MakeLabel("LIFELOGGING LIVE", 18, 14, 390, 18, 10f, FontStyle.Bold, dim);
                title.Font = new Font("Consolas", 10f, FontStyle.Bold);

                statusLabel = MakeLabel("LIVE", 18, 38, 390, 32, 20f, FontStyle.Bold, green);
                statusLabel.Font = new Font("Consolas", 20f, FontStyle.Bold);

                lastLabel = MakeLabel("No run yet", 18, 78, 390, 18, 9f, FontStyle.Regular, fg);
                lastLabel.Font = new Font("Consolas", 9f);

                nextLabel = MakeLabel("", 18, 98, 390, 18, 9f, FontStyle.Regular, dim);
                nextLabel.Font = new Font("Consolas", 9f);

                infoLabel = MakeLabel("", 18, 118, 390, 18, 8f, FontStyle.Regular, dim);
                infoLabel.Font = new Font("Consolas", 8f);

                runButton = MakeButton("RUN NOW", 18, 154, 90);
                runButton.Click += delegate { Program.RequestRun(); };
                pauseButton = MakeButton("PAUSE", 114, 154, 90);
                pauseButton.Click += delegate { Program.TogglePause(); };
                var logsButton = MakeButton("OPEN LOGS", 210, 154, 90);
                logsButton.Click += delegate { Program.OpenLogs(); };
                var siteButton = MakeButton("OPEN SITE", 306, 154, 100);
                siteButton.Click += delegate { Program.OpenSite(); };

                var foot = MakeLabel("Hidden runner · every 2 min · logs in state\\logs",
                    18, 204, 390, 18, 8f, FontStyle.Regular, dim);
                foot.Font = new Font("Consolas", 8f);

                Controls.Add(title);
                Controls.Add(statusLabel);
                Controls.Add(lastLabel);
                Controls.Add(nextLabel);
                Controls.Add(infoLabel);
                Controls.Add(runButton);
                Controls.Add(pauseButton);
                Controls.Add(logsButton);
                Controls.Add(siteButton);
                Controls.Add(foot);

                var menu = new ContextMenuStrip();
                var openItem = new ToolStripMenuItem("Open panel");
                openItem.Click += delegate { ShowFromTray(); };
                runItem = new ToolStripMenuItem("Run now");
                runItem.Click += delegate { Program.RequestRun(); };
                pauseItem = new ToolStripMenuItem("Pause");
                pauseItem.Click += delegate { Program.TogglePause(); };
                var logsItem = new ToolStripMenuItem("Open logs");
                logsItem.Click += delegate { Program.OpenLogs(); };
                var exitItem = new ToolStripMenuItem("Exit");
                exitItem.Click += delegate { Program.RequestExit(); };
                menu.Items.Add(openItem);
                menu.Items.Add(runItem);
                menu.Items.Add(pauseItem);
                menu.Items.Add(new ToolStripSeparator());
                menu.Items.Add(logsItem);
                menu.Items.Add(exitItem);

                tray = new NotifyIcon();
                tray.Icon = appIcon;
                tray.Text = "LifeLogging Live";
                tray.ContextMenuStrip = menu;
                tray.Visible = true;
                tray.DoubleClick += delegate { ShowFromTray(); };

                uiTimer = new System.Windows.Forms.Timer();
                uiTimer.Interval = 1000;
                uiTimer.Tick += delegate { UpdateUi(); };
                uiTimer.Start();
                UpdateUi();
                panelSize = Size;
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

            [DllImport("user32.dll")]
            private static extern bool IsIconic(IntPtr hWnd);

            [DllImport("user32.dll")]
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
                b.Size = new Size(w, 30);
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

            private void UpdateUi()
            {
                bool p;
                bool running;
                bool exitNow;
                DateTime? last;
                DateTime? next;
                int? code;
                double secs;
                int count;
                lock (sync)
                {
                    p = paused;
                    running = runInProgress;
                    exitNow = exiting;
                    last = lastRunAt;
                    next = nextRunAt;
                    code = lastExitCode;
                    secs = lastSeconds;
                    count = runCount;
                }

                string state;
                Color color;
                if (exitNow)
                {
                    state = "STOPPING";
                    color = dim;
                }
                else if (running)
                {
                    state = "RUNNING NOW";
                    color = cyan;
                }
                else if (p)
                {
                    state = "PAUSED";
                    color = amber;
                }
                else
                {
                    state = "LIVE";
                    color = green;
                }
                statusLabel.Text = state;
                statusLabel.ForeColor = color;

                if (running && last.HasValue)
                {
                    lastLabel.Text = "Running since " + last.Value.ToString("HH:mm:ss") + " …";
                    lastLabel.ForeColor = cyan;
                }
                else if (last.HasValue)
                {
                    string codeText = code.HasValue ? code.Value.ToString() : "—";
                    lastLabel.Text = string.Format("Last run  {0}  ·  exit {1}  ·  {2:0.0}s",
                        last.Value.ToString("HH:mm:ss"), codeText, secs);
                    lastLabel.ForeColor = (code.HasValue && code.Value == 0) ? fg : red;
                }
                else
                {
                    lastLabel.Text = "Last run  —";
                    lastLabel.ForeColor = dim;
                }

                if (running)
                {
                    nextLabel.Text = "Next run  after this one";
                }
                else if (p)
                {
                    nextLabel.Text = "Next run  paused";
                }
                else if (next.HasValue)
                {
                    TimeSpan left = next.Value - DateTime.Now;
                    if (left.TotalSeconds < 0)
                    {
                        left = TimeSpan.Zero;
                    }
                    nextLabel.Text = string.Format("Next run  {0}  ·  in {1}:{2:00}",
                        next.Value.ToString("HH:mm:ss"), (int)left.TotalMinutes, left.Seconds);
                }
                else
                {
                    nextLabel.Text = "Next run  —";
                }

                infoLabel.Text = string.Format("Runs {0}  ·  every 2 min  ·  auto-start on login", count);

                runButton.Enabled = !p && !running && !exitNow;
                pauseButton.Text = p ? "RESUME" : "PAUSE";
                pauseButton.Enabled = !exitNow;
                runItem.Enabled = runButton.Enabled;
                pauseItem.Text = p ? "Resume" : "Pause";

                string tip = "LifeLogging Live · " + state;
                if (!running && !p && next.HasValue)
                {
                    TimeSpan left = next.Value - DateTime.Now;
                    if (left.TotalSeconds > 0)
                    {
                        tip += string.Format(" · next {0}:{1:00}", (int)left.TotalMinutes, left.Seconds);
                    }
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
