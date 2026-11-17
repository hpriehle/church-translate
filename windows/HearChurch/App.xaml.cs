using HearChurch.Core;
using HearChurch.UI;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace HearChurch;

/// <summary>Hear Church for Windows (W2): one window (setup, then the operator page), Settings, and the icon in the
/// notification area that keeps the engine running when the window is closed. docs/windows-app.md.</summary>
public partial class App : Application
{
    public static AppModel Model { get; private set; } = null!;
    public static MainWindow? Main { get; private set; }
    public static Updates Updates { get; private set; } = null!;
    private Tray? _tray;
    private SettingsWindow? _settings;
    private Mutex? _single;
    private EventWaitHandle? _showSignal, _quitSignal;
    private bool _quitting;

    public App()
    {
        InitializeComponent();
        UnhandledException += (_, e) =>
        {
            try { File.AppendAllText(Path.Combine(AppPaths.Logs, "app-errors.log"), $"{DateTime.Now:s} {e.Exception}\n"); } catch { }
            e.Handled = true;
        };
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        var argv = Environment.GetCommandLineArgs();
        // One copy at a time: a second start brings the first one's window to the front.
        _single = new Mutex(true, "HearChurch.SingleInstance", out var first);
        _showSignal = new EventWaitHandle(false, EventResetMode.AutoReset, "HearChurch.Show");
        // `HearChurch.exe --quit` asks the running copy to quit (unless a meeting runs): CI's update test, scripts.
        _quitSignal = new EventWaitHandle(false, EventResetMode.AutoReset, "HearChurch.Quit");
        if (!first || argv.Contains("--quit"))
        {
            if (!first) (argv.Contains("--quit") ? _quitSignal : _showSignal).Set();
            Environment.Exit(0);
            return;
        }
        Directory.CreateDirectory(AppPaths.Logs);
        if (Updates.ApplyAtStart(argv.Skip(1).ToArray()))   // a downloaded update: installed, then Hear Church restarts
        {
            Environment.Exit(0);
            return;
        }
        var dq = DispatcherQueue.GetForCurrentThread();
        void Post(Action a) => dq.TryEnqueue(() => a());
        Model = new AppModel(Post);
        Model.OpenSettings = OpenSettings;
        Updates = new Updates(() => Model.Running);
        Main = new MainWindow(Model);
        Main.AppWindow.Closing += (_, e) =>
        {
            if (_quitting) return;
            e.Cancel = true;   // closing the window keeps Hear Church (and a meeting) running in the notification area
            Main.AppWindow.Hide();
            Model.WindowVisible = false;
        };
        _tray = new Tray(Path.Combine(AppContext.BaseDirectory, "Assets", "HearChurch.ico"), ShowMain, TrayMenu);
        Model.Notify = (title, text) => _tray?.Notify(title, text);
        Model.Changed += () => _tray?.SetTip(Model.Running ? $"Hear Church · translating · {Listeners(Model.Listeners)}" : "Hear Church");

        new Thread(() =>
        {
            while (_showSignal.WaitOne()) dq.TryEnqueue(ShowMain);
        }) { IsBackground = true, Name = "show-signal" }.Start();
        new Thread(() =>
        {
            while (_quitSignal.WaitOne())
                dq.TryEnqueue(() =>
                {
                    if (Model.Running) Updates.Log("--quit ignored: a meeting is running");
                    else Quit(0);
                });
        }) { IsBackground = true, Name = "quit-signal" }.Start();

        Model.Launch();
        var tick = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        var n = 0;
        tick.Tick += (_, _) =>
        {
            Main?.Tick();
            if (++n % 10 == 0)
            {
                Model.Tick();
                Updates.Tick();
            }
        };
        tick.Start();

        if (argv.Contains("--background") && Model.Profile.SetupDone) Model.WindowVisible = false;   // started with Windows
        else Main.Activate();

        if (Environment.GetEnvironmentVariable("HEARCHURCH_APP_SCREENSHOTS") is { Length: > 0 } dir)
            _ = new Screenshots(Model, Main, OpenSettings, () => _settings, Quit).Run(dir);
    }

    private static string Listeners(int n) => n == 1 ? "1 listener" : $"{n} listeners";

    private void ShowMain()
    {
        if (Main == null) return;
        Main.AppWindow.Show();
        Main.Activate();
        Model.WindowVisible = true;
    }

    private List<Tray.Item?> TrayMenu()
    {
        var items = new List<Tray.Item?>();
        if (Model.Profile.SetupDone)
        {
            items.Add(new Tray.Item(Model.Running ? $"Translating · {Listeners(Model.Engine.Status?.Engine.Client?.Listeners?.Captions ?? 0)}" : "Not translating", null));
            items.Add(Model.Running
                ? new Tray.Item("Stop", () => _ = Model.StopMeeting())
                : new Tray.Item("Start", () => _ = Model.StartMeeting(), Enabled: Model.CanStart));
            items.Add(null);
        }
        items.Add(new Tray.Item("Open Hear Church", ShowMain));
        items.Add(new Tray.Item("Open Hear Church when this PC starts", () => LoginItem.Set(!LoginItem.Enabled), LoginItem.Enabled));
        items.Add(new Tray.Item(Updates.Checking ? "Checking for Updates…" : "Check for Updates…", Updates.Checking ? null : CheckForUpdates));
        items.Add(null);
        items.Add(new Tray.Item("Quit Hear Church", () => _ = AskQuit()));
        return items;
    }

    public void OpenSettings()
    {
        if (_settings == null)
        {
            _settings = new SettingsWindow(Model);
            _settings.Closed += (_, _) => _settings = null;
        }
        _settings.Activate();
    }

    /// <summary>Quitting during a meeting stops it for every listener: ask first (the Mac's wording).</summary>
    private async Task AskQuit()
    {
        if (Model.Running && Main != null)
        {
            ShowMain();
            var dlg = new ContentDialog
            {
                Title = "A meeting is being translated",
                Content = "Quitting stops the translation for every listener.",
                PrimaryButtonText = "Stop and Quit",
                CloseButtonText = "Keep Translating",
                DefaultButton = ContentDialogButton.Close,
                XamlRoot = Main.Content.XamlRoot,
            };
            if (await dlg.ShowAsync() != ContentDialogResult.Primary) return;
            await Model.StopMeeting();
        }
        Quit(0);
    }

    /// <summary>"Check for Updates…" in the icon's menu: the answer comes as a notification.</summary>
    private async void CheckForUpdates()
    {
        var text = await Updates.Check();
        _tray?.Notify("Hear Church", text, warning: false);
    }

    private void Quit(int code)
    {
        _quitting = true;
        try { Model.Engine.Stop(); } catch { }
        Updates.ApplyOnQuit();   // a downloaded update goes in once this process has exited
        try { Model.Mic.Dispose(); } catch { }
        _tray?.Dispose();
        _tray = null;
        WinSystem.KeepAwake(false);
        try { _settings?.Close(); Main?.Close(); } catch { }
        Environment.Exit(code);
    }
}
