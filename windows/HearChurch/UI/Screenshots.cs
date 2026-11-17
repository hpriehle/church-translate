using System.Runtime.InteropServices.WindowsRuntime;
using System.Text.Json;
using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.Graphics.Imaging;
using Windows.Storage.Streams;

namespace HearChurch.UI;

/// <summary>CI only (HEARCHURCH_APP_SCREENSHOTS=folder, scripts/windows_screens.py): walks the app through its main
/// screens for real and saves a picture of each. Signs in with a pairing code from a local test hub
/// (HEARCHURCH_APP_DEMO_CODE), finishes setup, waits for the engine and its downloads, plays a recorded sermon
/// (HEARCHURCH_APP_DEMO_FILE, in the data folder's samples\) as the meeting, opens Settings, then quits.
/// Writes screens.json with what happened; exit code 0 only if every screen was captured.</summary>
public sealed class Screenshots
{
    private readonly AppModel _model;
    private readonly MainWindow _window;
    private readonly Action _openSettings;
    private readonly Func<SettingsWindow?> _settings;
    private readonly Action<int> _quit;
    private readonly List<object> _steps = new();
    private string _dir = "";
    private readonly DateTime _t0 = DateTime.UtcNow;

    public Screenshots(AppModel model, MainWindow window, Action openSettings, Func<SettingsWindow?> settings, Action<int> quit)
    {
        _model = model;
        _window = window;
        _openSettings = openSettings;
        _settings = settings;
        _quit = quit;
    }

    private void Log(string what, bool ok = true, string detail = "")
    {
        _steps.Add(new { t = Math.Round((DateTime.UtcNow - _t0).TotalSeconds, 1), step = what, ok, detail });
        try
        {
            File.WriteAllText(Path.Combine(_dir, "screens.json"), JsonSerializer.Serialize(_steps, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch { }
    }

    private static Task Wait(double seconds) => Task.Delay(TimeSpan.FromSeconds(seconds));

    private async Task<bool> Until(Func<bool> cond, double timeout, double every = 1)
    {
        var end = DateTime.UtcNow.AddSeconds(timeout);
        while (DateTime.UtcNow < end)
        {
            if (cond()) return true;
            await Wait(every);
        }
        return cond();
    }

    public async Task Run(string dir)
    {
        _dir = dir;
        Directory.CreateDirectory(dir);
        var failures = 0;
        try
        {
            await Wait(5);
            // 1. Sign-in (the first launch).
            if (_window.Onboarding is { } ob && ob.Current == OnboardingView.Step.Welcome)
            {
                failures += await Capture(_window.Root, "1-sign-in.png") ? 0 : 1;
                var code = Environment.GetEnvironmentVariable("HEARCHURCH_APP_DEMO_CODE") ?? "";
                ob.ChooseCodeWay();
                await Wait(1);
                await ob.SignInWithCode(code);
                var signedIn = await Until(() => _window.Onboarding?.Current == OnboardingView.Step.Languages, 30);
                Log("signed in with a pairing code", signedIn, _model.Profile.Slug);
                if (!signedIn) { failures++; await Capture(_window.Root, "error-sign-in.png"); }
            }
            else Log("sign-in screen", false, "already signed in: start with an empty HEARCHURCH_HOME");

            // 2. Setup: languages (speaker English, translation French: already downloaded by the CI script), input.
            if (_window.Onboarding is { } ob2)
            {
                await Until(() => _model.Engine.Packages.Count > 0, 60);
                await Wait(2);
                await Capture(_window.Root, "2-setup-languages.png");
                while (_window.Onboarding is { } o && o.Current != OnboardingView.Step.Welcome)
                {
                    o.Next();
                    await Wait(1.5);
                }
                Log("setup finished", _model.Profile.SetupDone);
            }

            // 3. The main window, ready for a meeting.
            var ready = await Until(() => _model.Engine.State == Engine.RunState.Running && _model.Engine.Settings != null
                                          && _model.NeededDownloads.All(_model.IsReady) && _model.IsReady("speech"), 600, 2);
            Log("engine running, languages downloaded", ready, string.Join(", ", _model.Engine.Packages.Select(p => $"{p.Id} {p.State}")));
            await Wait(4);
            failures += await Capture(_window.Root, "3-main-idle.png") ? 0 : 1;
            Log("checks before the meeting", true, string.Join(" · ", _model.Checks.Select(c => $"{c.State}: {c.Title}")));

            // 4. A meeting: the recorded sermon instead of the microphone.
            var file = Environment.GetEnvironmentVariable("HEARCHURCH_APP_DEMO_FILE") ?? "sermon.mp3";
            await _model.StartMeeting(file);
            var started = await Until(() => _model.Running, 30);
            Log("meeting started", started, _model.Message ?? "");
            var captioned = await Until(() => _model.Engine.Chunks.Count(c => c.Langs.Values.Any(o => o.CaptionLag != null)) >= 6, 240, 2);
            Log("transcript with translations", captioned, $"{_model.Engine.Chunks.Count} lines, behind: "
                + string.Join(", ", (_model.Engine.Status?.Pipeline.BehindS ?? new()).Select(b => $"{b.Key} {b.Value:0.0}s")));
            await Wait(2);
            failures += await Capture(_window.Root, "4-main-meeting.png") ? 0 : 1;
            if (!captioned) failures++;

            // 5. Settings.
            _openSettings();
            await Wait(3);
            if (_settings() is { } sw) failures += await Capture(sw.Root, "5-settings.png") ? 0 : 1;
            else { Log("settings window", false); failures++; }

            await _model.StopMeeting();
            await Until(() => !_model.Running, 30);
            Log("meeting stopped", !_model.Running);
        }
        catch (Exception e)
        {
            failures++;
            Log("unexpected error", false, e.ToString());
        }
        Log(failures == 0 ? "done" : $"done with {failures} failure(s)", failures == 0);
        _quit(failures == 0 ? 0 : 1);
    }

    /// <summary>The window's content as a PNG (RenderTargetBitmap: no desktop capture needed).</summary>
    private async Task<bool> Capture(UIElement root, string name)
    {
        try
        {
            var rtb = new RenderTargetBitmap();
            await rtb.RenderAsync(root);
            var pixels = (await rtb.GetPixelsAsync()).ToArray();
            using var stream = new InMemoryRandomAccessStream();
            var enc = await BitmapEncoder.CreateAsync(BitmapEncoder.PngEncoderId, stream);
            enc.SetPixelData(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied, (uint)rtb.PixelWidth, (uint)rtb.PixelHeight, 96, 96, pixels);
            await enc.FlushAsync();
            stream.Seek(0);
            using (var fs = File.Create(Path.Combine(_dir, name))) await stream.AsStreamForRead().CopyToAsync(fs);
            Log("captured " + name, true, $"{rtb.PixelWidth}×{rtb.PixelHeight}");
            return true;
        }
        catch (Exception e)
        {
            Log("captured " + name, false, e.Message);
            return false;
        }
    }
}
