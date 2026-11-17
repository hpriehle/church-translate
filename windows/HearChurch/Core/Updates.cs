using Velopack;
using Velopack.Locators;

namespace HearChurch.Core;

/// <summary>Automatic updates (Velopack, docs/windows-release.md), the Mac's Sparkle in Windows terms: check at launch
/// and every 6 hours while no meeting runs, download in the background, apply when Hear Church quits (or at its next
/// start), never during a meeting. A meeting starting cancels a download in progress; it resumes after the meeting.
/// The feed is hearchurch.com/download/windows/&lt;channel&gt;/, the channel ("dev" or "public") the one the installed
/// package was built for. "UpdateURL" in app-settings.json (a URL or a folder) points it elsewhere (CI, testing).
/// A copy that wasn't installed (an unzipped folder) never updates.</summary>
public sealed class Updates
{
    public static readonly TimeSpan Every = TimeSpan.FromHours(6);

    private readonly Func<bool> _meeting;
    private readonly UpdateManager? _mgr;
    private CancellationTokenSource? _cts;
    private DateTime _last = DateTime.MinValue;

    /// <summary>The version downloaded and waiting for Hear Church to quit, or null.</summary>
    public string? Ready { get; private set; }
    public bool Checking => _cts != null;
    public event Action? Changed;

    public Updates(Func<bool> meetingRunning)
    {
        _meeting = meetingRunning;
        try
        {
            var mgr = new UpdateManager(Feed);
            if (mgr.IsInstalled)
            {
                _mgr = mgr;
                Ready = Display(mgr.UpdatePendingRestart?.Version?.ToString());
                Log($"installed {mgr.CurrentVersion}, channel {Channel}, feed {Feed}");
            }
        }
        catch (Exception e)
        {
            Log($"no updates: {e.Message}");
        }
    }

    public bool Installed => _mgr != null;

    /// <summary>The channel the installed package was built for (vpk pack --channel).</summary>
    public static string Channel
    {
        get
        {
            try { return VelopackLocator.Current.Channel is { Length: > 0 } c ? c : "public"; }
            catch { return "public"; }
        }
    }

    public static string Feed => Prefs.GetString("UpdateURL") is { Length: > 0 } u ? u : $"https://hearchurch.com/download/windows/{Channel}";

    /// <summary>"0.4.0 (12)": the Mac's version number and this PC's build number. Dev builds are packaged as
    /// 0.4.0-dev.12, public ones as 0.4.0 with the build in the file version (docs/windows-release.md).</summary>
    public string Version
    {
        get
        {
            string? installed = null;
            try { installed = _mgr?.CurrentVersion?.ToString(); } catch { }
            return Display(installed) ?? AssemblyVersion;
        }
    }

    private static string AssemblyVersion
    {
        get
        {
            var v = typeof(Updates).Assembly.GetName().Version;
            return v == null ? "?" : v.Revision > 0 ? $"{v.Major}.{v.Minor}.{v.Build} ({v.Revision})" : $"{v.Major}.{v.Minor}.{v.Build}";
        }
    }

    private static string? Display(string? semver)
    {
        if (string.IsNullOrEmpty(semver)) return null;
        var dash = semver.IndexOf('-');
        if (dash < 0)
        {
            var v = typeof(Updates).Assembly.GetName().Version;
            return v != null && v.Revision > 0 && semver == $"{v.Major}.{v.Minor}.{v.Build}" ? $"{semver} ({v.Revision})" : semver;
        }
        var build = semver[(semver.LastIndexOf('.') + 1)..];
        return int.TryParse(build, out _) ? $"{semver[..dash]} ({build})" : semver;
    }

    /// <summary>Every 10 s from the app's timer: a check is due at launch and 6 hours after the last one, never during
    /// a meeting; a meeting starting stops a download.</summary>
    public void Tick()
    {
        if (_mgr == null) return;
        if (_meeting())
        {
            if (_cts is { IsCancellationRequested: false } c) { Log("meeting started: download stopped"); c.Cancel(); }
            return;
        }
        if (!Checking && Ready == null && DateTime.UtcNow - _last >= Every) _ = Check();
    }

    /// <summary>Checks the feed and downloads a newer version. Returns what to tell the operator.</summary>
    public async Task<string> Check()
    {
        if (_mgr == null) return "Updates come with the installed app (hearchurch.com/download).";
        if (Ready != null) return $"Hear Church {Ready} is ready. It is installed the next time Hear Church quits or this PC restarts.";
        if (_meeting()) return "Hear Church checks for updates after the meeting.";
        if (Checking) return "Checking for updates…";
        var cts = _cts = new CancellationTokenSource();
        _last = DateTime.UtcNow;
        Changed?.Invoke();
        var mgr = _mgr;
        try
        {
            var info = await Task.Run(() => mgr.CheckForUpdatesAsync());
            if (info == null)
            {
                Log("up to date");
                return $"Hear Church {Version} is up to date.";
            }
            var target = info.TargetFullRelease.Version.ToString();
            Log($"downloading {target}");
            await Task.Run(() => mgr.DownloadUpdatesAsync(info, _ => { }, cts.Token));
            Ready = Display(target);
            Log($"downloaded {target}: applied when Hear Church quits");
            return $"Hear Church {Ready} is ready. It is installed the next time Hear Church quits or this PC restarts.";
        }
        catch (OperationCanceledException)
        {
            _last = DateTime.MinValue;   // again after the meeting
            return "Hear Church checks for updates after the meeting.";
        }
        catch (Exception e)
        {
            Log($"check failed: {e.GetType().Name}: {e.Message}");
            return "Couldn't check for updates. Check the internet connection and try again later.";
        }
        finally
        {
            _cts = null;
            cts.Dispose();
            Changed?.Invoke();
        }
    }

    /// <summary>At start, in the only running copy (no meeting yet): an update downloaded before the PC restarted or the
    /// app was ended goes in now, and Hear Church starts again with the same arguments. True: this process is exiting.</summary>
    public static bool ApplyAtStart(string[] args)
    {
        try
        {
            var mgr = new UpdateManager(Feed);
            if (!mgr.IsInstalled || mgr.UpdatePendingRestart is not { } pending) return false;
            Log($"applying {pending.Version} at start");
            mgr.ApplyUpdatesAndRestart(pending, args);
            return true;
        }
        catch (Exception e)
        {
            Log($"apply at start failed: {e.Message}");
            return false;
        }
    }

    /// <summary>On quit (no meeting runs by then): hand a downloaded update to Velopack's updater, which waits for
    /// this process to exit, installs it silently and doesn't restart the app.</summary>
    public void ApplyOnQuit()
    {
        try
        {
            if (_mgr?.UpdatePendingRestart is { } pending)
            {
                Log($"applying {pending.Version} on quit");
                _mgr.WaitExitThenApplyUpdates(pending, true, false, Array.Empty<string>());
            }
        }
        catch (Exception e)
        {
            Log($"apply failed: {e.Message}");
        }
    }

    /// <summary>Velopack's uninstall hook (30 s at most): Windows mustn't start a removed app at login. The data folder
    /// (%LOCALAPPDATA%\HearChurch) and the saved sign-in stay, as on the Mac.</summary>
    public static void BeforeUninstall()
    {
        LoginItem.Set(false);
        Log("uninstalling");
    }

    public static void Log(string line)
    {
        try
        {
            Directory.CreateDirectory(AppPaths.Logs);
            File.AppendAllText(Path.Combine(AppPaths.Logs, "updates.log"), $"{DateTime.Now:s} {line}\n");
        }
        catch { }
    }
}
