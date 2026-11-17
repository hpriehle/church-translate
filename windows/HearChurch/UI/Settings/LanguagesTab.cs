using System.Text.Json.Nodes;
using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Windows.Media.Core;
using Windows.Media.Playback;

namespace HearChurch.UI.Settings;

/// <summary>Every language: search for it, download it, delete it to get the disk space back, or open it to pick its
/// voice. Only downloaded languages show in the main window's speaker and translation grids, so this is where they're
/// added. Speech recognition (and Wolof speech recognition) at the bottom.</summary>
public sealed class LanguagesTab : SettingsTab
{
    private readonly TextBox _search;
    private OutputLanguage? _open;
    private string? _problem;
    private string? _openState;
    private MediaPlayer? _player;
    private string? _playing;
    private string? _listenProblem;

    public LanguagesTab(AppModel model) : base(model, "Languages")
    {
        _search = new TextBox { PlaceholderText = "Search languages" };
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(_search, "Search languages");
        _search.TextChanged += (_, _) => Refresh();
        Add(_search);
        Add(Key, Build);
        Refresh();
    }

    public override void Refresh()
    {
        _search.Visibility = _open == null && Model.Engine.Packages.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        if (_open != null)
        {
            var state = Model.Package(_open.Id)?.State;
            if (state != _openState) { _openState = state; _ = Model.Engine.LoadSettings(); }   // a finished download brings its voices
        }
        base.Refresh();
    }

    public override void Leaving() => StopPlaying();

    private string Key() =>
        $"{_open?.Id}|{_problem}|{_search.Text}|{Model.Running}|{string.Join(",", Model.Speakers)}>{string.Join(",", Model.Targets)}|"
        + string.Join(";", Model.Engine.Packages.Select(p => $"{p.Id}:{p.State}:{(int)p.DoneMb}"))
        + $"|{Model.Engine.Settings?.VoiceKey}|{_playing}|{_listenProblem}";

    private UIElement Build()
    {
        if (Model.Engine.Packages.Count == 0) return Ui.T("The translation engine is starting…", Ui.Body, Ui.Secondary);
        return _open is { } l ? Detail(l) : List();
    }

    // ---------------------------------------------------------------- the list

    private IEnumerable<OutputLanguage> Matching(IEnumerable<OutputLanguage> list)
    {
        var q = _search.Text.Trim();
        return q.Length == 0 ? list : list.Where(l => l.Name.Contains(q, StringComparison.OrdinalIgnoreCase) || l.Native.Contains(q, StringComparison.OrdinalIgnoreCase));
    }

    private UIElement List()
    {
        var all = OutputLanguage.All;
        var downloaded = Matching(all.Where(l => l.Available && Model.IsReady(l.Id))).ToList();
        var others = Matching(all.Where(l => l.Available && !Model.IsReady(l.Id)))
            .OrderBy(l => Model.Package(l.Id)?.State == "downloading" ? 0 : 1).ThenBy(l => l.Name).ToList();
        var unusable = Matching(all.Where(l => !l.Available)).ToList();

        var p = new StackPanel { Spacing = 22 };
        if (downloaded.Count > 0) p.Children.Add(Ui.Group("Downloaded", null, downloaded.Select(Row).ToArray()));
        var more = others.Select(Row).ToList();
        if (downloaded.Count == 0 && others.Count == 0 && unusable.Count == 0)
            more.Add(Ui.T($"No language called “{_search.Text.Trim()}”.", Ui.Body, Ui.Secondary));
        else if (others.Count == 0)
            more.Add(Ui.T("Every language is downloaded.", Ui.Body, Ui.Secondary));
        p.Children.Add(Ui.Group("More languages",
            "Downloaded languages appear in the main window's speaker and translation grids, above Start meeting. Download a language here to add it there; remove it to free the space.",
            more.ToArray()));
        if (unusable.Count > 0)
            p.Children.Add(Ui.Group("Not on this PC", null, unusable.Select(l =>
                (UIElement)Ui.Column(2, Ui.T(l.Name, Ui.Body, Ui.Secondary), Ui.T(l.UnavailableReason ?? "", Ui.Callout, Ui.Secondary))).ToArray()));
        if (_problem != null) p.Children.Add(Ui.T(_problem, Ui.Callout, Ui.Orange));

        var wolof = Model.Package(OutputLanguage.WolofSpeechPackage);
        p.Children.Add(Ui.Group(null, null,
            Ui.Field(Ui.Column(0, Ui.T("Speech recognition"), Ui.T("Needed for every language", Ui.Caption, Ui.Secondary)),
                     OnboardingView.PackageBadge(Model.Package("speech"), OutputLanguage.SpeechDownloadMB)),
            Ui.Field(Ui.Column(0, Ui.T("Wolof speech recognition"), Ui.T("Needed when someone speaks Wolof", Ui.Caption, Ui.Secondary)),
                     WolofRight(wolof))));
        return p;
    }

    private UIElement WolofRight(PackageStatus? wolof)
    {
        var r = Ui.Row(10, OnboardingView.PackageBadge(wolof, OutputLanguage.WolofSpeechDownloadMB));
        if (PackageAction(OutputLanguage.WolofSpeechPackage, async () => { _problem = await Model.RemoveWolofSpeech(); Rebuild(); }) is { } a)
            r.Children.Add(a);
        return r;
    }

    private UIElement Row(OutputLanguage l)
    {
        var name = new Button
        {
            Content = Ui.Row(8, Ui.T(l.Name, wrap: false), Ui.T(l.Native, Ui.Body, Ui.Secondary, wrap: false)),
            Background = new SolidColorBrush(Microsoft.UI.Colors.Transparent),
            BorderThickness = new Thickness(0),
            Padding = new Thickness(0, 4, 0, 4),
            HorizontalAlignment = HorizontalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Left,
        };
        name.Click += (_, _) => OpenLanguage(l);
        var chevron = new Button
        {
            Content = Ui.Icon("", 12, Ui.Tertiary),
            Background = new SolidColorBrush(Microsoft.UI.Colors.Transparent),
            BorderThickness = new Thickness(0),
        };
        Ui.Tip(chevron, "Choose its voice");
        chevron.Click += (_, _) => OpenLanguage(l);
        var right = Ui.Row(10, OnboardingView.PackageBadge(Model.Package(l.Id), l.SizeMB));
        if (PackageAction(l.Id, () => AskToRemove(l)) is { } a) right.Children.Add(a);
        right.Children.Add(chevron);
        return Ui.Field(name, right);
    }

    /// <summary>Download, Try again or Remove, whichever fits the package's state.</summary>
    private UIElement? PackageAction(string id, Func<Task> remove) => Model.Package(id)?.State switch
    {
        "ready" => Ui.Button("Remove", () => _ = remove()),
        "downloading" => null,
        "failed" => Ui.Button("Try again", () => Model.DownloadLanguage(id)),
        _ => Ui.Button("Download", () => Model.DownloadLanguage(id)),
    };

    /// <summary>Opens one language's page (its download and voices).</summary>
    public void OpenLanguage(string code)
    {
        if (OutputLanguage.All.FirstOrDefault(l => l.Id == code) is { } l) OpenLanguage(l);
    }

    private void OpenLanguage(OutputLanguage l)
    {
        _problem = null;
        _listenProblem = null;
        _open = l;
        _openState = null;
        Refresh();
    }

    /// <summary>The button always answers: with the question, or with the reason it can't be removed right now.</summary>
    private async Task AskToRemove(OutputLanguage l)
    {
        if (Model.Running && (Model.IsOn(l.Id, LanguageRole.Speaker) || Model.IsOn(l.Id, LanguageRole.Target)))
        {
            _problem = $"{l.Name} is in this meeting: switch it off first, or stop the meeting.";
            Refresh();
            return;
        }
        _problem = null;
        Refresh();
        var size = Format.Size(Model.Package(l.Id)?.SizeMb is double mb and > 0 ? mb : l.SizeMB);
        if (!await Ui.Confirm(XamlRoot, $"Remove {l.Name}?",
                $"This deletes its voices and frees {size}. Your glossary for it is kept, and you can download it again at any time.", "Remove"))
            return;
        _problem = await Model.RemoveLanguage(l.Id);
        Rebuild();
    }

    // ---------------------------------------------------------------- one language: its download and its voice

    private UIElement Detail(OutputLanguage l)
    {
        var p = new StackPanel { Spacing = 18 };
        p.Children.Add(Ui.Link("‹ Languages", () => { StopPlaying(); _problem = null; _open = null; Refresh(); }));
        var right = Ui.Row(10, OnboardingView.PackageBadge(Model.Package(l.Id), l.SizeMB));
        if (PackageAction(l.Id, () => AskToRemove(l)) is { } a) right.Children.Add(a);
        p.Children.Add(Ui.Group(null, null,
            Ui.Field(Ui.Column(2, Ui.T(l.Name, Ui.Title3, semibold: true), Ui.T(l.Native, Ui.Body, Ui.Secondary)), right),
            _problem == null ? null : Ui.T(_problem, Ui.Callout, Ui.Orange)));
        p.Children.Add(Ui.Group("Voice", null, Voices(l).ToArray()));
        return p;
    }

    private List<UIElement> Voices(OutputLanguage l)
    {
        var o = new List<UIElement>();
        var s = Model.Engine.Settings;
        var options = s?.VoiceCatalog.GetValueOrDefault(l.Id) ?? new();
        var usable = Model.Package(l.Id)?.State == "ready" ? s?.Voices.GetValueOrDefault(l.Id) ?? new() : new();
        var current = Selected(l, usable);
        foreach (var option in options)
        {
            UIElement left;
            if (usable.Any(u => u.Key == option.Key))
            {
                var rb = new RadioButton { Content = option.Label, GroupName = "voice-" + l.Id, IsChecked = option.Key == current };
                rb.Click += (_, _) => _ = Choose(l, option);
                left = rb;
            }
            else left = Ui.T(option.Label);
            var playing = _playing == option.Key;
            var listen = new Button { Content = Ui.Row(6, Ui.Icon(playing ? "" : "", 12), Ui.T(playing ? "Stop" : "Listen", wrap: false)) };
            Ui.Tip(listen, "Hear this voice before choosing it");
            listen.Click += (_, _) => Listen(l, option);
            o.Add(Ui.Field(left, listen));
        }
        if (usable.Count == 0)
            o.Add(Ui.T($"Listen to each voice now. Download {l.Name} to choose one.", Ui.Callout, Ui.Secondary));
        if (_listenProblem != null) o.Add(Ui.T(_listenProblem, Ui.Callout, Ui.Orange));
        return o;
    }

    private string Selected(OutputLanguage l, List<EngineSettings.VoiceOption> options)
    {
        var ids = options.Select(o => o.Key).ToList();
        if (Model.Engine.Settings?.Settings is not { } s) return ids.FirstOrDefault() ?? "";
        var current = l.Id == "fr" ? $"{s.Tts.Engine}:{s.Tts.Voice}"
            : s.Voices.TryGetValue(l.Id, out var v) ? $"{v.Engine}:{v.Voice}" : null;
        return current != null && ids.Contains(current) ? current : ids.FirstOrDefault() ?? "";
    }

    private async Task Choose(OutputLanguage l, EngineSettings.VoiceOption o)
    {
        var v = new JsonObject { ["engine"] = o.Engine, ["voice"] = o.Voice };
        await Model.Engine.Set(l.Id == "fr" ? new JsonObject { ["tts"] = v } : new JsonObject { ["voices"] = new JsonObject { [l.Id] = v } });
    }

    /// <summary>A recording shipped with the app (the Mac's voice samples), so no download is needed. Plays on this PC only.</summary>
    private void Listen(OutputLanguage l, EngineSettings.VoiceOption o)
    {
        var was = _playing == o.Key;
        StopPlaying();
        _listenProblem = null;
        if (!was)
        {
            var file = Path.Combine(AppContext.BaseDirectory, "Assets", "VoiceSamples", $"sample-{l.Id}-{o.Voice}.m4a");
            try
            {
                if (!File.Exists(file)) throw new FileNotFoundException();
                var p = new MediaPlayer { Source = MediaSource.CreateFromUri(new Uri(file)) };
                p.MediaEnded += (_, _) => DispatcherQueue.TryEnqueue(() => { if (_player == p) { StopPlaying(); Refresh(); } });
                p.MediaFailed += (_, _) => DispatcherQueue.TryEnqueue(() =>
                {
                    if (_player != p) return;
                    StopPlaying();
                    _listenProblem = "Couldn't play that voice.";
                    Refresh();
                });
                p.Play();
                _player = p;
                _playing = o.Key;
            }
            catch { _listenProblem = "Couldn't play that voice."; }
        }
        Refresh();
    }

    private void StopPlaying()
    {
        try { _player?.Pause(); _player?.Dispose(); } catch { }
        _player = null;
        _playing = null;
    }
}
