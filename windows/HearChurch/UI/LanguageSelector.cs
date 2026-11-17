using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;

namespace HearChurch.UI;

/// <summary>The meeting's languages (docs/v3-languages.md): "Select speaker language" and "Select translation language",
/// two grids of toggles with the direction between them. Large before a meeting, compact in the sidebar during one, and
/// in setup (every language). The Mac's LanguageSelector.swift.</summary>
public sealed class LanguageSelector : StackPanel
{
    private readonly AppModel _model;
    private readonly bool _compact, _everyLanguage, _showTitle;
    private readonly Slot _body;
    private string? _note;

    public LanguageSelector(AppModel model, bool compact = false, bool showTitle = true, bool everyLanguage = false)
    {
        _model = model;
        _compact = compact;
        _showTitle = showTitle;
        _everyLanguage = everyLanguage;
        Spacing = compact ? 8 : 14;
        _body = new Slot(Key, Build);
        Children.Add(_body);
        Refresh();
    }

    public void Refresh() => _body.Refresh();

    private string Key() => string.Join(",", _model.Speakers) + "|" + string.Join(",", _model.Targets) + "|" + _model.Running + "|"
        + _note + "|" + _model.Engine.Packages.Count + "|"
        + string.Join(";", _model.Engine.Packages.Select(p => $"{p.Id}:{p.State}:{(int)(p.DoneMb / Math.Max(1, p.SizeMb) * 20)}"));

    private UIElement Build()
    {
        var p = new StackPanel { Spacing = _compact ? 8 : 14, HorizontalAlignment = HorizontalAlignment.Stretch };
        if (_showTitle)
        {
            var title = Ui.T("Translation", _compact ? 16 : Ui.LargeTitle, bold: true);
            title.HorizontalAlignment = _compact ? HorizontalAlignment.Left : HorizontalAlignment.Center;
            p.Children.Add(title);
        }
        p.Children.Add(Section("Select speaker language:", Shown(OutputLanguage.Speakers, LanguageRole.Speaker), LanguageRole.Speaker));
        var arrow = Ui.Icon("", _compact ? 14 : 24, Ui.Accent);   // ArrowDown
        arrow.HorizontalAlignment = HorizontalAlignment.Center;
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(arrow, "translated into");
        p.Children.Add(arrow);
        p.Children.Add(Section("Select translation language:", Shown(OutputLanguage.All, LanguageRole.Target), LanguageRole.Target));
        if (_note != null) p.Children.Add(Ui.T(_note, Ui.Callout, Ui.Orange));
        if (!_compact && Summary() is { } s)
        {
            var t = Ui.T(s, Ui.Callout, Ui.Secondary);
            t.TextAlignment = TextAlignment.Center;
            t.HorizontalAlignment = HorizontalAlignment.Center;
            p.Children.Add(t);
        }
        if (!_everyLanguage)
        {
            var link = Ui.Link(_compact ? "Add or remove languages…" : "Add or remove languages in Settings…", () => _model.OpenSettings?.Invoke());
            link.HorizontalAlignment = _compact ? HorizontalAlignment.Left : HorizontalAlignment.Center;
            link.FontSize = Ui.Callout;
            p.Children.Add(link);
        }
        return p;
    }

    /// <summary>Downloaded languages, and the ones this meeting already has. In setup, every language. Wolof is always
    /// offered as a speaker language: picking it downloads its speech recognition.</summary>
    private List<OutputLanguage> Shown(IReadOnlyList<OutputLanguage> languages, LanguageRole role) =>
        _everyLanguage ? languages.ToList()
            : languages.Where(l => _model.IsReady(l.Id, role) || _model.IsOn(l.Id, role) || (role == LanguageRole.Speaker && l.Id == "wo")).ToList();

    private UIElement Section(string title, List<OutputLanguage> languages, LanguageRole role)
    {
        var p = new StackPanel { Spacing = _compact ? 6 : 8 };
        p.Children.Add(Ui.T(title, _compact ? Ui.Callout : Ui.Title3, semibold: true));
        if (languages.Count == 0)
            p.Children.Add(Ui.T(_model.Engine.Packages.Count == 0 ? "The translation engine is starting…"
                                                                   : "No language downloaded yet: add one in Settings → Languages.", Ui.Callout, Ui.Secondary));
        var grid = new CenteredGrid { MinCellWidth = _compact ? 92 : 128, Spacing = _compact ? 6 : 8 };
        foreach (var l in languages) grid.Children.Add(Cell(l, role));
        p.Children.Add(grid);
        return p;
    }

    /// <summary>One language toggle: on (accent), off, or not downloaded yet (with its size, or the download's progress).</summary>
    private UIElement Cell(OutputLanguage language, LanguageRole role)
    {
        var on = _model.IsOn(language.Id, role);
        var pkg = _model.Package(OutputLanguage.PackageId(language.Id, role));
        var available = language.AvailableAs(role);
        var state = pkg?.State ?? (_model.Engine.Packages.Count == 0 ? "unknown" : "missing");
        var onText = on ? new SolidColorBrush(Microsoft.UI.Colors.White) : null;

        var content = new StackPanel { Spacing = 2, HorizontalAlignment = HorizontalAlignment.Center };
        var top = Ui.Row(6, Ui.T(_compact ? language.Name : language.Native, _compact ? Ui.Callout : Ui.Body, onText, wrap: false));
        top.HorizontalAlignment = HorizontalAlignment.Center;
        if (on) top.Children.Add(Ui.Icon("", 11, onText));   // CheckMark
        content.Children.Add(top);
        if (!_compact || state != "ready")
        {
            UIElement second;
            var dim = on ? new SolidColorBrush(Windows.UI.Color.FromArgb(217, 255, 255, 255)) : Ui.Secondary;
            switch (state)
            {
                case "ready": second = Ui.T(language.Name, Ui.Caption, dim, wrap: false); break;
                case "downloading":
                    second = new ProgressBar { Minimum = 0, Maximum = 1, Width = 70, Value = Math.Min(1, (pkg?.DoneMb ?? 0) / Math.Max(1, pkg?.SizeMb ?? 1)) };
                    break;
                case "failed": second = Ui.Label("Download failed", "", Ui.Orange, Ui.Caption); break;
                case "unknown": second = Ui.T(language.Name, Ui.Caption, Ui.Secondary, wrap: false); break;
                default: second = Ui.Label($"Download {Format.Size(pkg?.SizeMb ?? language.SizeMBAs(role))}", "", dim, Ui.Caption); break;
            }
            if (second is FrameworkElement fe) fe.HorizontalAlignment = HorizontalAlignment.Center;
            content.Children.Add(second);
        }
        var b = new Button
        {
            Content = content,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Center,
            Padding = _compact ? new Thickness(8, 5, 8, 5) : new Thickness(10, 8, 10, 8),
            IsEnabled = available,
            Opacity = available ? 1 : 0.45,
            CornerRadius = new CornerRadius(8),
        };
        if (on) b.Style = (Style)Application.Current.Resources["AccentButtonStyle"];
        Ui.Tip(b, Help(language, role, state, on));
        b.Click += (_, _) =>
        {
            _note = _model.Toggle(language.Id, role);
            Refresh();
        };
        return b;
    }

    private static string Help(OutputLanguage language, LanguageRole role, string state, bool on)
    {
        if (language.UnavailableReasonAs(role) is { } why) return why;
        var what = OutputLanguage.PackageId(language.Id, role) == OutputLanguage.WolofSpeechPackage ? "Wolof speech recognition" : language.Name;
        return state switch
        {
            "ready" => on ? $"Click to switch {language.Name} off" : $"Click to switch {language.Name} on",
            "downloading" => $"{what} is downloading",
            _ => $"{what} needs to be downloaded: click to download it",
        };
    }

    /// <summary>"Russian and Latvian → English and Latvian", with what pass-through means.</summary>
    private string? Summary()
    {
        var from = _model.Speakers.Select(OutputLanguage.NameOf).ToList();
        var to = _model.Targets.Select(OutputLanguage.NameOf).ToList();
        if (from.Count == 0 || to.Count == 0) return null;
        var both = _model.Targets.Where(_model.Speakers.Contains).Select(OutputLanguage.NameOf).ToList();
        var s = $"{OutputLanguage.List(from)} → {OutputLanguage.List(to)}.";
        if (both.Count > 0) s += $" {OutputLanguage.List(both)} listeners hear {(both.Count == 1 ? "that" : "those")} speakers in the original.";
        return s;
    }
}
