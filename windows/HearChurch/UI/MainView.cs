using System.Text.Json.Nodes;
using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Media;
using Windows.ApplicationModel.DataTransfer;

namespace HearChurch.UI;

/// <summary>The operator page (the Mac's MainView.swift): Start meeting at the top left, the checks and controls on the
/// left with Listeners and the QR code pinned at the bottom, and what is being said (with its translations) filling the
/// rest. Before a meeting the languages sit large in the main pane; during one, under Gain in the sidebar.</summary>
public sealed class MainView : Grid
{
    private readonly AppModel _model;
    private readonly List<Slot> _slots = new();
    private readonly LevelMeter _meter = new(140);
    private readonly TextBlock _elapsed = Ui.T("", Ui.Body, Ui.Secondary, wrap: false);
    private readonly Slot _main;
    private TranscriptView? _transcript;
    private LanguageSelector? _bigSelector, _smallSelector;

    public MainView(AppModel model)
    {
        _model = model;
        Background = Ui.WindowBackground;
        foreach (var h in new[] { GridLength.Auto, GridLength.Auto, GridLength.Auto, GridLength.Auto, GridLength.Auto, new GridLength(1, GridUnitType.Star) })
            RowDefinitions.Add(new RowDefinition { Height = h });

        Add(Slot(() => _model.MicWarning ?? "", MicWarningBanner), 0);
        Add(Slot(() => _model.Engine.State == Engine.RunState.Failed ? _model.Engine.FailReason : "", () => EngineFailedBanner(_model, XamlRoot)), 1);
        Add(TopBar(), 2);
        Add(Ui.Divider(), 3);
        Add(Slot(DownloadKey, () => DownloadBar(_model)), 4);

        var body = new Grid();
        body.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(330) });
        body.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        body.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        var side = Sidebar();
        body.Children.Add(side);
        var div = Ui.Divider(true);
        SetColumn(div, 1);
        body.Children.Add(div);
        _main = new Slot(() => _model.Running ? "meeting" : "idle" + (_model.Engine.Chunks.Count > 0), () => _model.Running ? MeetingPane() : IdlePane());
        SetColumn(_main, 2);
        body.Children.Add(_main);
        Add(body, 5);

        _model.Mic.LevelChanged += MicLevel;
        Loaded += (_, _) => { _model.MeterAppeared(); Refresh(); };
        Unloaded += (_, _) => { _model.MeterDisappeared(); _model.Mic.LevelChanged -= MicLevel; };
        Refresh();
    }

    private void MicLevel()
    {
        if (!_model.Running) _meter.SetDb(_model.Mic.Db);
    }

    private void Add(UIElement e, int row)
    {
        SetRow((FrameworkElement)e, row);
        Children.Add(e);
    }

    private Slot Slot(Func<string> key, Func<UIElement?> build)
    {
        var s = new Slot(key, build);
        _slots.Add(s);
        return s;
    }

    public void Refresh()
    {
        foreach (var s in _slots) s.Refresh();
        _main.Refresh();
        _transcript?.Refresh();
        _bigSelector?.Refresh();
        _smallSelector?.Refresh();
        if (_model.Running) _meter.SetDb(_model.Engine.Status?.Pipeline.LevelDb ?? -80);
        Tick();
    }

    /// <summary>Once a second: the meeting's elapsed time.</summary>
    public void Tick()
    {
        _elapsed.Text = _model.Running && _model.MeetingStarted is { } t ? Format.Elapsed(DateTime.UtcNow - t) : "";
    }

    private string DownloadKey() => string.Join(";", _model.Downloads.Select(p => $"{p.Id}:{p.State}:{(int)p.DoneMb}"));

    // ---------------------------------------------------------------- banners

    private UIElement? MicWarningBanner() => _model.MicWarning is { } w
        ? new InfoBar { Severity = InfoBarSeverity.Error, IsOpen = true, IsClosable = false, Title = w }
        : null;

    /// <summary>Shown (main window and setup) when the translation engine is stopped for good.</summary>
    public static UIElement? EngineFailedBanner(AppModel model, XamlRoot? root)
    {
        if (model.Engine.State != Engine.RunState.Failed) return null;
        var actions = Ui.Row(8, Ui.Button("Restart engine", model.RelaunchEngine), Ui.Button("Show log", ShowLog));
        return new InfoBar
        {
            Severity = InfoBarSeverity.Error, IsOpen = true, IsClosable = false, Message = model.Engine.FailReason,
            Content = actions,
        };
    }

    public static void ShowLog()
    {
        try
        {
            System.Diagnostics.Process.Start("explorer.exe", File.Exists(Engine.LogPath) ? $"/select,\"{Engine.LogPath}\"" : $"\"{AppPaths.Logs}\"");
        }
        catch { }
    }

    /// <summary>The thin bar under the top bar while voices and speech recognition download.</summary>
    public static UIElement? DownloadBar(AppModel model)
    {
        var items = model.Downloads;
        if (items.Count == 0) return null;
        var active = items.Where(p => p.State == "downloading").ToList();
        double done = active.Sum(p => p.DoneMb), total = active.Sum(p => p.SizeMb);
        var g = new Grid { Padding = new Thickness(16, 8, 16, 8), Background = Ui.Layer, RowSpacing = 4 };
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        UIElement right;
        if (active.Count == 0)
        {
            g.Children.Add(Ui.Label("Download failed: " + string.Join(", ", items.Select(p => p.Name)), "", Ui.Orange, Ui.Callout));
            right = Ui.Button("Try again", () => _ = model.Engine.Download(model.NeededDownloads));
        }
        else
        {
            g.Children.Add(Ui.T("Downloading " + string.Join(" and ", active.Select(p => p.Name.ToLowerInvariant())), Ui.Callout, wrap: false));
            right = Ui.T($"{Format.Size(done)} of {Format.Size(total)}", Ui.Callout, Ui.Secondary, wrap: false);
            var bar = new ProgressBar { Minimum = 0, Maximum = 1, Value = total > 0 ? done / total : 0 };
            SetRow(bar, 1);
            SetColumnSpan(bar, 2);
            g.Children.Add(bar);
        }
        SetColumn((FrameworkElement)right, 1);
        g.Children.Add(right);
        return Ui.Column(0, g, Ui.Divider());
    }

    // ---------------------------------------------------------------- top bar

    private UIElement TopBar()
    {
        var g = new Grid { Padding = new Thickness(16, 10, 16, 10), ColumnSpacing = 16 };
        foreach (var w in new[] { GridLength.Auto, GridLength.Auto, GridLength.Auto, new GridLength(1, GridUnitType.Star), GridLength.Auto, GridLength.Auto, GridLength.Auto, GridLength.Auto, GridLength.Auto })
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = w });
        var col = 0;
        void Put(UIElement e) { SetColumn((FrameworkElement)e, col++); ((FrameworkElement)e).VerticalAlignment = VerticalAlignment.Center; g.Children.Add(e); }

        Put(Slot(() => $"{_model.Running}|{_model.CanStart}", StartButton));
        Put(Ui.Row(8, Slot(MeetingStateKey, MeetingState), _elapsed));
        Put(Ui.Row(6, Ui.Icon("", 14, Ui.Secondary), _meter));   // Microphone
        col++;   // spacer
        Put(Slot(() => $"{_model.Listeners}|{_model.Running}", () =>
            Ui.Label($"{_model.Listeners} listening", "", _model.Running ? Ui.Primary : Ui.Secondary)));   // People
        Put(Slot(ConnectionKey, ConnectionBadge));
        Put(new Border { Width = 1, Height = 22, Background = Ui.DividerBrush });
        Put(Slot(() => _model.Profile.Name + "|" + _model.Profile.Stake, () =>
        {
            var p = new StackPanel { HorizontalAlignment = HorizontalAlignment.Right };
            var n = Ui.T(_model.Profile.Name, 15, semibold: true, wrap: false);
            n.HorizontalAlignment = HorizontalAlignment.Right;
            p.Children.Add(n);
            if (_model.Profile.Stake.Length > 0)
            {
                var s = Ui.T(_model.Profile.Stake, Ui.Caption, Ui.Secondary, wrap: false);
                s.HorizontalAlignment = HorizontalAlignment.Right;
                p.Children.Add(s);
            }
            return p;
        }));
        var gear = new Button { Content = Ui.Icon("", 16), Padding = new Thickness(6), Background = new SolidColorBrush(Microsoft.UI.Colors.Transparent), BorderThickness = new Thickness(0) };
        Ui.Tip(gear, "Settings");
        gear.Click += (_, _) => _model.OpenSettings?.Invoke();
        Put(gear);
        return g;
    }

    private UIElement StartButton()
    {
        var running = _model.Running;
        var label = Ui.Row(8, Ui.Icon(running ? "" : "", 16, new SolidColorBrush(Microsoft.UI.Colors.White)),   // Stop / Play
                           Ui.T(running ? "Stop meeting" : "Start meeting", 17, new SolidColorBrush(Microsoft.UI.Colors.White), semibold: true, wrap: false));
        label.HorizontalAlignment = HorizontalAlignment.Center;
        var b = new Button { Content = label, MinWidth = 170, MinHeight = 38, Padding = new Thickness(16, 4, 16, 4) };
        b.Style = (Style)Application.Current.Resources["AccentButtonStyle"];
        var c = running ? Windows.UI.Color.FromArgb(255, 0xC4, 0x2B, 0x1C) : Windows.UI.Color.FromArgb(255, 0x10, 0x7C, 0x10);
        var hover = running ? Windows.UI.Color.FromArgb(255, 0xA8, 0x23, 0x17) : Windows.UI.Color.FromArgb(255, 0x0E, 0x6B, 0x0E);
        b.Resources["AccentButtonBackground"] = new SolidColorBrush(c);
        b.Resources["AccentButtonBackgroundPointerOver"] = new SolidColorBrush(hover);
        b.Resources["AccentButtonBackgroundPressed"] = new SolidColorBrush(hover);
        b.Resources["AccentButtonForeground"] = new SolidColorBrush(Microsoft.UI.Colors.White);
        b.IsEnabled = running || _model.CanStart;
        if (!running && !_model.CanStart) Ui.Tip(b, "Fix the items marked in red first");
        b.Click += async (_, _) =>
        {
            if (_model.Running)
            {
                var dlg = new ContentDialog
                {
                    Title = "Stop translating for every listener?",
                    PrimaryButtonText = "Stop meeting",
                    CloseButtonText = "Cancel",
                    DefaultButton = ContentDialogButton.Close,
                    XamlRoot = XamlRoot,
                };
                if (await dlg.ShowAsync() == ContentDialogResult.Primary) await _model.StopMeeting();
            }
            else
            {
                b.IsEnabled = false;
                await _model.StartMeeting();
                b.IsEnabled = true;
            }
        };
        return b;
    }

    private string MeetingStateKey() => $"{_model.Running}|{_model.Engine.Status?.Engine.Client?.Live}|{_model.Engine.State}|{_model.CanStart}|{_model.Problems}";

    private UIElement MeetingState()
    {
        if (_model.Running)
        {
            var live = _model.Engine.Status?.Engine.Client?.Live == true;
            return new Border
            {
                Background = live ? Ui.Red : new SolidColorBrush(Windows.UI.Color.FromArgb(255, 0xE8, 0x7A, 0x00)),
                CornerRadius = new CornerRadius(10),
                Padding = new Thickness(8, 2, 8, 3),
                Child = Ui.T("LIVE", Ui.Caption, new SolidColorBrush(Microsoft.UI.Colors.White), bold: true, wrap: false),
            };
        }
        if (_model.Engine.State != Engine.RunState.Running) return Ui.Label("Starting…", "", Ui.Secondary);   // Stopwatch
        if (_model.CanStart) return Ui.Label("Ready", "", Ui.Green);   // Completed
        var n = _model.Problems;
        return Ui.Label(n == 1 ? "1 thing to fix" : $"{n} things to fix", "", Ui.Orange);   // Error
    }

    private string ConnectionKey()
    {
        var link = _model.Engine.Status?.Engine.Client;
        return $"{_model.Online}|{_model.LocalSharing}|{link?.Connected}|{link == null}|{link?.Error}|{link?.Notice}";
    }

    private UIElement ConnectionBadge()
    {
        var link = _model.Engine.Status?.Engine.Client;
        var (icon, color, text) =
            !_model.Online ? ("", Ui.Red, "Offline")
            : _model.LocalSharing ? ("", Ui.Green, "This Wi-Fi only")
            : link?.Connected == true ? ("", Ui.Green, AppPaths.HubHost)
            : link == null ? ("", Ui.Secondary, "Not linked")
            : ("", Ui.Orange, "Connecting…");
        var l = Ui.Label(text, icon, color);
        Ui.Tip(l, link?.Error ?? link?.Notice);
        return l;
    }

    // ---------------------------------------------------------------- sidebar

    private UIElement Sidebar()
    {
        var g = new Grid { Background = Ui.Layer };
        g.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        var p = new StackPanel { Spacing = 18, Padding = new Thickness(16) };
        p.Children.Add(Slot(() => _model.Message ?? "", () => _model.Message is { } m ? Ui.T(m, Ui.Callout, Ui.Red) : null));
        p.Children.Add(Slot(ChecksKey, () => _model.Running ? DuringMeeting() : BeforeMeeting()));
        p.Children.Add(Slot(SoundKey, () => SoundSection(_model, compactGain: false)));
        p.Children.Add(Slot(() => _model.Running.ToString(), () =>
        {
            _smallSelector = _model.Running ? new LanguageSelector(_model, compact: true, showTitle: false) : null;
            return _smallSelector == null ? null : Ui.Section("Languages", _smallSelector);
        }));
        p.Children.Add(Slot(LanguageNotesKey, LanguageNotes));
        p.Children.Add(Slot(StatsKey, Stats));
        g.Children.Add(new ScrollViewer { Content = p, VerticalScrollBarVisibility = ScrollBarVisibility.Auto });
        var d = Ui.Divider();
        SetRow(d, 1);
        g.Children.Add(d);
        var listeners = Slot(ListenersKey, ListenersSection);
        listeners.Padding = new Thickness(16);
        SetRow(listeners, 2);
        g.Children.Add(listeners);
        return g;
    }

    private string ChecksKey()
    {
        if (!_model.Running) return "before|" + _model.OnBattery + "|" + string.Join(";", _model.Checks.Select(c => $"{c.Id}:{c.Title}:{c.State}:{c.Detail}"));
        var st = _model.Engine.Status;
        return "during|" + string.Join(";", _model.Targets.Select(c => $"{c}:{st?.TranslateBy.GetValueOrDefault(c)?.Claude.State}:{st?.TranslateBy.GetValueOrDefault(c)?.LastError}"))
            + $"|{st?.Source}|{(int)(st?.Pipeline.BacklogS ?? 0)}|{st?.Pipeline.LastError}|{st?.Engine.Client?.Notice}|{_model.MicName}";
    }

    private UIElement BeforeMeeting()
    {
        var s = Ui.Section("Before the meeting");
        foreach (var c in _model.Checks) s.Children.Add(CheckRow(c));
        if (_model.OnBattery) s.Children.Add(Ui.T("Keep the lid open and the PC plugged in until the meeting ends.", Ui.Callout, Ui.Secondary));
        return s;
    }

    public static UIElement CheckRow(Check check)
    {
        var g = new Grid { ColumnSpacing = 10 };
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(16) });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        UIElement icon = check.State switch
        {
            Check.Kind.Ok => Ui.Icon("", 14, Ui.Green),
            Check.Kind.Warn => Ui.Icon("", 14, Ui.Orange),
            Check.Kind.Bad => Ui.Icon("", 14, Ui.Red),
            _ => new ProgressRing { IsActive = true, Width = 12, Height = 12 },
        };
        ((FrameworkElement)icon).VerticalAlignment = VerticalAlignment.Top;
        ((FrameworkElement)icon).Margin = new Thickness(0, 3, 0, 0);
        g.Children.Add(icon);
        var text = Ui.Column(2, Ui.T(check.Title));
        if (check.Detail.Length > 0) text.Children.Add(Ui.T(check.Detail, Ui.Callout, Ui.Secondary));
        SetColumn(text, 1);
        g.Children.Add(text);
        if (check.Action is { } a)
        {
            var b = Ui.Button(a.Title, a.Run);
            b.Padding = new Thickness(8, 2, 8, 3);
            b.VerticalAlignment = VerticalAlignment.Top;
            SetColumn(b, 2);
            g.Children.Add(b);
        }
        return g;
    }

    private UIElement DuringMeeting()
    {
        var st = _model.Engine.Status;
        var s = Ui.Section("Translating");
        foreach (var code in _model.Targets)
        {
            var t = st?.TranslateBy.GetValueOrDefault(code);
            var row = new Grid();
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            row.Children.Add(Ui.T(OutputLanguage.NameOf(code)));
            TextBlock right;
            if (st?.Source == code)
            {
                right = Ui.T("Original", color: Ui.Secondary, wrap: false);
                Ui.Tip(right, $"The speaker is speaking {OutputLanguage.NameOf(code)}: it goes out as spoken");
            }
            else
            {
                var state = t?.Claude.State;
                right = Ui.T(state switch { "ready" or "busy" => "Translating", "starting" => "Starting…", _ => "Paused, retrying" },
                             color: state is "ready" or "busy" ? Ui.Green : Ui.Orange, wrap: false);
            }
            SetColumn(right, 1);
            row.Children.Add(right);
            s.Children.Add(row);
            if (!string.IsNullOrEmpty(t?.LastError)) s.Children.Add(Ui.T(t.LastError, Ui.Caption, Ui.Orange));
        }
        if (st?.Pipeline.BacklogS is { } b && b > 6)
            s.Children.Add(Ui.T($"Voices are {(int)b} s behind the speaker; they speed up to catch up.", Ui.Callout, Ui.Orange));
        if (!string.IsNullOrEmpty(st?.Pipeline.LastError)) s.Children.Add(Ui.T(st.Pipeline.LastError, Ui.Callout, Ui.Red));
        if (!string.IsNullOrEmpty(st?.Engine.Client?.Notice)) s.Children.Add(Ui.T(st.Engine.Client.Notice!, Ui.Callout, Ui.Orange));
        if (_model.MicName is { } mic)
        {
            var l = Ui.Label(mic, "", Ui.Secondary, Ui.Caption);
            Ui.Tip(l, "The microphone this meeting is using");
            s.Children.Add(l);
        }
        s.Children.Add(Ui.T("Keep the lid open and the PC plugged in until the meeting ends.", Ui.Callout, Ui.Secondary));
        return s;
    }

    private string SoundKey()
    {
        var s = _model.Engine.Settings;
        return s == null ? "" : $"{_model.Running}|{_model.MissingInput}|{s.Settings.Audio.InputDevice}|{s.Settings.Audio.InputChannel}|"
            + string.Join(",", s.Devices.Select(d => $"{d.Name}:{d.Channels}:{d.Default}"));
    }

    /// <summary>The input (the hearing-assist receiver's line-in or USB input, by name), its channel, and the gain.</summary>
    public static UIElement SoundSection(AppModel model, bool compactGain)
    {
        var s = Ui.Section("Sound");
        if (model.Engine.Settings is not { } settings)
        {
            s.Children.Add(new ProgressRing { IsActive = true, Width = 16, Height = 16, HorizontalAlignment = HorizontalAlignment.Left });
            return s;
        }
        s.Children.Add(InputPicker(model, settings));
        var audio = settings.Settings.Audio;
        if (settings.Devices.FirstOrDefault(d => d.Name == audio.InputDevice) is { Channels: > 1 } dev)
        {
            var ch = new ComboBox { Header = "Channel", HorizontalAlignment = HorizontalAlignment.Stretch, IsEnabled = !model.Running };
            ch.Items.Add(new ComboBoxItem { Content = "Mix all", Tag = 0 });
            for (var i = 1; i <= dev.Channels; i++) ch.Items.Add(new ComboBoxItem { Content = i.ToString(), Tag = i });
            ch.SelectedIndex = Math.Min(audio.InputChannel, dev.Channels);
            ch.SelectionChanged += (_, _) =>
            {
                if (ch.SelectedItem is ComboBoxItem { Tag: int n } && n != model.Engine.Settings?.Settings.Audio.InputChannel)
                    _ = model.Engine.Set(new JsonObject { ["audio"] = new JsonObject { ["input_channel"] = n } });
            };
            s.Children.Add(ch);
        }
        s.Children.Add(GainSlider(model, audio.InputGain));
        return s;
    }

    public static ComboBox InputPicker(AppModel model, EngineSettings settings)
    {
        var audio = settings.Settings.Audio;
        var box = new ComboBox { Header = "Input", HorizontalAlignment = HorizontalAlignment.Stretch, IsEnabled = !model.Running };
        var def = settings.Devices.FirstOrDefault(d => d.Default == true)?.Name ?? "none";
        box.Items.Add(new ComboBoxItem { Content = $"PC default ({def})", Tag = "" });
        if (model.MissingInput is { } missing) box.Items.Add(new ComboBoxItem { Content = $"{missing} (not connected)", Tag = missing });
        foreach (var d in settings.Devices) box.Items.Add(new ComboBoxItem { Content = d.Name, Tag = d.Name });
        box.SelectedItem = box.Items.OfType<ComboBoxItem>().FirstOrDefault(i => (string)i.Tag == audio.InputDevice) ?? box.Items[0];
        box.SelectionChanged += (_, _) =>
        {
            if (box.SelectedItem is ComboBoxItem { Tag: string name } && name != model.SelectedInput) _ = model.ChooseInput(name);
        };
        return box;
    }

    private static UIElement GainSlider(AppModel model, double gain)
    {
        var g = new Grid { ColumnSpacing = 8 };
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(40) });
        var label = Ui.T("Gain", wrap: false);
        label.VerticalAlignment = VerticalAlignment.Center;
        g.Children.Add(label);
        var slider = new Slider { Minimum = 0.5, Maximum = 4, StepFrequency = 0.1, Value = gain, IsThumbToolTipEnabled = false, VerticalAlignment = VerticalAlignment.Center };
        SetColumn(slider, 1);
        g.Children.Add(slider);
        var value = Ui.T(Format.F(gain, "0.0") + "×", wrap: false);
        value.VerticalAlignment = VerticalAlignment.Center;
        value.HorizontalAlignment = HorizontalAlignment.Right;
        SetColumn(value, 2);
        g.Children.Add(value);
        var commit = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(400) };
        commit.Tick += (_, _) =>
        {
            commit.Stop();
            _ = model.Engine.Set(new JsonObject { ["audio"] = new JsonObject { ["input_gain"] = Math.Round(slider.Value, 1) } });
        };
        slider.ValueChanged += (_, e) =>
        {
            value.Text = Format.F(e.NewValue, "0.0") + "×";
            commit.Stop();
            commit.Start();
        };
        return g;
    }

    // ---------------------------------------------------------------- during a meeting: languages heard, stats

    private readonly HashSet<string> _seen = new();

    private string LanguageNotesKey()
    {
        if (!_model.Running) return "";
        var heard = string.Join(",", (_model.Engine.Status?.AutoSpeakers ?? new()).Where(c => _model.Speakers.Contains(c) && !_seen.Contains(c)));
        var failed = _model.Engine.Packages.FirstOrDefault(p => p.State == "failed");
        return $"{heard}|{_model.Engine.Status?.Source}|{_model.Speakers.Count}|{failed?.Name}:{failed?.Error}";
    }

    private UIElement? LanguageNotes()
    {
        if (!_model.Running) return null;
        var p = new StackPanel { Spacing = 6, Margin = new Thickness(0, -10, 0, 0) };
        foreach (var code in (_model.Engine.Status?.AutoSpeakers ?? new()).Where(c => _model.Speakers.Contains(c) && !_seen.Contains(c)))
        {
            var undo = Ui.Link("Undo", () => { if (_model.Toggle(code, LanguageRole.Speaker) is { } why) { _model.Message = why; _model.Emit(); } });
            undo.FontSize = Ui.Caption;
            var keep = new Button { Content = Ui.Icon("", 10), Padding = new Thickness(4), Background = new SolidColorBrush(Microsoft.UI.Colors.Transparent), BorderThickness = new Thickness(0) };
            Ui.Tip(keep, "Keep it");
            keep.Click += (_, _) => { _seen.Add(code); Refresh(); };
            p.Children.Add(Ui.Row(6, Ui.Icon("", 12, Ui.Orange), Ui.T($"{OutputLanguage.NameOf(code)} detected: added to speaker languages", Ui.Caption), undo, keep));
        }
        if (_model.Engine.Status?.Source is { } src && _model.Speakers.Count > 1)
            p.Children.Add(Ui.T($"Speaking now: {OutputLanguage.NameOf(src)}", Ui.Caption, Ui.Secondary));
        if (_model.Engine.Packages.FirstOrDefault(x => x.State == "failed") is { } f)
            p.Children.Add(Ui.T($"{f.Name}: {f.Error}", Ui.Caption, Ui.Orange));
        return p.Children.Count == 0 ? null : p;
    }

    private bool _statsOpen = Prefs.GetString("StatsOpen") != "0";

    private string StatsKey()
    {
        if (!_model.Running) return "";
        var p = _model.Engine.Status?.Pipeline;
        var m = p?.MeetingStats;
        return $"{_statsOpen}|{m?.Lines}|{m?.Skipped}|{(int)(m?.SpeechS ?? 0) / 30}|{p?.SttSkipped}|{p?.SttModel}|{p?.SttSteppedDown}|{Math.Round(p?.SttRtf ?? 0, 1)}|"
            + string.Join(";", _model.Targets.Select(c => $"{c}:{_model.ListenersOn(c)}:{Math.Round(p?.BehindS?.GetValueOrDefault(c) ?? -1)}:{m?.Langs.GetValueOrDefault(c)?.Captioned}"));
    }

    private UIElement? Stats()
    {
        if (!_model.Running) return null;
        var p = _model.Engine.Status?.Pipeline;
        var m = p?.MeetingStats;
        var s = new StackPanel { Spacing = 8 };
        var head = new Button { Background = new SolidColorBrush(Microsoft.UI.Colors.Transparent), BorderThickness = new Thickness(0), Padding = new Thickness(0),
                                Content = Ui.Row(4, Ui.T("STATS", Ui.Caption, Ui.Secondary, semibold: true), Ui.Icon(_statsOpen ? "" : "", 10, Ui.Secondary)) };
        head.Click += (_, _) => { _statsOpen = !_statsOpen; Prefs.Set("StatsOpen", _statsOpen ? "1" : "0"); Refresh(); };
        s.Children.Add(head);
        if (!_statsOpen) return s;
        if (m != null)
        {
            s.Children.Add(Line("Heard", $"{m.Lines} lines · {Format.Minutes(m.SpeechS)} of speech"));
            var behind = p?.SttSkipped ?? 0;
            if (m.Skipped > 0 || behind > 0)
            {
                var bits = new List<string>();
                if (m.Skipped - behind > 0) bits.Add($"{m.Skipped - behind} no words");
                if (behind > 0) bits.Add($"{behind} to catch up");
                if (m.Failed > 0) bits.Add($"{m.Failed} failed");
                s.Children.Add(Line("Skipped", string.Join(" · ", bits), behind > 0,
                    "Stretches with no words (noise, music) are skipped. On a slow PC, speech recognition also skips to the newest speech when it falls behind."));
            }
        }
        var model = p?.SttModel is { Length: > 0 } name
            ? name.Replace("whisper-", "") + (p.SttEngine == "faster-whisper" && p.SttRtf is > 0 ? $" · {Format.F(p.SttRtf.Value, "0.00")}×" : "")
              + (string.IsNullOrEmpty(p.SttSteppedDown) ? "" : " (smaller)")
            : p?.SttReady == true ? "Ready" : "Loading…";
        s.Children.Add(Line("Speech model", model, !string.IsNullOrEmpty(p?.SttSteppedDown),
            string.IsNullOrEmpty(p?.SttSteppedDown) ? "The speech recognition model in use" : p!.SttSteppedDown));
        foreach (var code in _model.Targets)
        {
            var l = m?.Langs.GetValueOrDefault(code);
            var top = new Grid();
            top.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            top.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var nameRow = Ui.Row(6, Ui.T(OutputLanguage.NameOf(code), Ui.Callout, semibold: true, wrap: false));
            if (_model.ListenersOn(code) is { } n)
            {
                var who = Ui.Label(n.ToString(), "", Ui.Secondary, Ui.Caption);   // Contact
                Ui.Tip(who, $"Phones on {OutputLanguage.NameOf(code)} now");
                nameRow.Children.Add(who);
            }
            top.Children.Add(nameRow);
            if (p?.BehindS?.GetValueOrDefault(code) is { } b && p.BehindS.ContainsKey(code))
            {
                var bt = Ui.T($"{Format.F(b, "0")} s behind", Ui.Caption, b > 15 ? Ui.Red : b > 8 ? Ui.Orange : Ui.Secondary, wrap: false);
                Ui.Tip(bt, "The latest line's delay behind the speaker");
                SetColumn(bt, 1);
                top.Children.Add(bt);
            }
            var block = Ui.Column(2, top);
            if (l is { Captioned: > 0 })
            {
                var text = $"{l.Captioned} lines · captions {Format.Seconds(l.CaptionP50)} (95%: {Format.Seconds(l.CaptionP95)})";
                if (l.Aired > 0) text += $" · voice {Format.Seconds(l.AudioP50)} (95%: {Format.Seconds(l.AudioP95)})";
                var dt = Ui.T(text, Ui.Caption, Ui.Secondary);
                Ui.Tip(dt, "Captions and voices after the speaker finished a sentence: typical (median) and slowest 5%");
                block.Children.Add(dt);
            }
            s.Children.Add(block);
        }
        return s;
    }

    private static UIElement Line(string key, string value, bool warn = false, string help = "")
    {
        var g = new Grid { ColumnSpacing = 8 };
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        g.Children.Add(Ui.T(key, Ui.Callout, Ui.Secondary, wrap: false));
        var v = Ui.T(value, Ui.Callout, warn ? Ui.Orange : Ui.Primary);
        v.TextAlignment = TextAlignment.Right;
        SetColumn(v, 1);
        g.Children.Add(v);
        Ui.Tip(g, help);
        return g;
    }

    // ---------------------------------------------------------------- listeners

    private bool _copied;

    private string ListenersKey() => $"{_model.LocalSharing}|{_model.Running}|{_model.ListenerUrl}|{_copied}";

    private UIElement ListenersSection()
    {
        var s = Ui.Section("Listeners");
        var cloud = new ToggleButton { Content = "hearchurch.com", IsChecked = !_model.LocalSharing, HorizontalAlignment = HorizontalAlignment.Stretch };
        var local = new ToggleButton { Content = "This Wi-Fi only", IsChecked = _model.LocalSharing, HorizontalAlignment = HorizontalAlignment.Stretch };
        cloud.Click += (_, _) => { cloud.IsChecked = true; if (_model.LocalSharing) _ = _model.SetSharing(false); };
        local.Click += (_, _) => { local.IsChecked = true; if (!_model.LocalSharing) _ = _model.SetSharing(true); };
        var seg = new Grid { ColumnSpacing = 4, IsHitTestVisible = !_model.Running, Opacity = _model.Running ? 0.6 : 1 };
        seg.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        seg.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        seg.Children.Add(cloud);
        SetColumn(local, 1);
        seg.Children.Add(local);
        if (_model.Running) Ui.Tip(seg, "Stop the meeting to change how it is shared");
        s.Children.Add(seg);
        s.Children.Add(Ui.T(_model.LocalSharing
            ? "Private: only phones on the same Wi-Fi as this PC, with this meeting's QR code. A new code after every meeting."
            : "Public: anyone with the link can listen, from anywhere.", Ui.Callout, Ui.Secondary));
        if (_model.ListenerUrl is { } url)
        {
            var row = new Grid { ColumnSpacing = 12 };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.Children.Add(Ui.Qr(url, 110));
            var right = new StackPanel { Spacing = 8 };
            right.Children.Add(Ui.T("Members scan this, or open:", Ui.Callout, Ui.Secondary));
            var link = new HyperlinkButton { NavigateUri = new Uri(url), Padding = new Thickness(0),
                                             Content = Ui.T(url.Replace("https://", "").Replace("http://", ""), Ui.Callout) };
            right.Children.Add(link);
            right.Children.Add(Ui.Button(_copied ? "Copied" : "Copy link", () =>
            {
                var dp = new DataPackage();
                dp.SetText(url);
                Clipboard.SetContent(dp);
                _copied = true;
                Refresh();
            }));
            SetColumn(right, 1);
            row.Children.Add(right);
            s.Children.Add(row);
            var test = Ui.Button("Play a test sentence", () => _ = _model.Engine.Post("api/test-voice"));
            Ui.Tip(test, "Every listening phone hears a short sentence in its language");
            s.Children.Add(Ui.Row(8, Ui.Button("Print a sign", () => AppModel.Open(_model.Engine.BaseUrl + "/print")), test));
        }
        else
            s.Children.Add(Ui.T(_model.LocalSharing ? "Waiting for the translation engine…" : "This PC isn't linked to hearchurch.com yet.", Ui.Callout, Ui.Secondary));
        return s;
    }

    // ---------------------------------------------------------------- main pane

    /// <summary>Before a meeting: the languages, large; the last meeting's transcript below.</summary>
    private UIElement IdlePane()
    {
        _smallSelector = null;
        var g = new Grid();
        g.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        _bigSelector = new LanguageSelector(_model) { MaxWidth = 760, Padding = new Thickness(24), HorizontalAlignment = HorizontalAlignment.Stretch };
        g.Children.Add(new ScrollViewer { Content = _bigSelector, VerticalScrollBarVisibility = ScrollBarVisibility.Auto });
        _transcript = null;
        if (_model.Engine.Chunks.Count > 0)
        {
            g.RowDefinitions[0].Height = new GridLength(1, GridUnitType.Star);
            g.RowDefinitions[1].Height = new GridLength(1, GridUnitType.Star);
            _transcript = new TranscriptView(_model, "Last meeting");
            var p = new Grid();
            p.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            p.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            p.Children.Add(Ui.Divider());
            SetRow(_transcript, 1);
            p.Children.Add(_transcript);
            SetRow(p, 1);
            g.Children.Add(p);
        }
        return g;
    }

    private UIElement MeetingPane()
    {
        _bigSelector = null;
        _transcript = new TranscriptView(_model);
        return _transcript;
    }

    public void Detach() => _model.Mic.LevelChanged -= MicLevel;
}
