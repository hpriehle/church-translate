using System.Text.Json.Nodes;
using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;

namespace HearChurch.UI.Settings;

/// <summary>The names and words the translator must translate a fixed way (app/config.py validate_glossary). Each entry
/// is {"en": …, "fr": …}; "fr" holds the translation whatever the language. English comes first: it has no list of its
/// own (translations into English read each spoken language's list the other way round), so its page only holds the
/// names kept as they are.</summary>
public sealed class GlossaryTab : SettingsTab
{
    private sealed class Pair
    {
        public required TextBox En;
        public required TextBox To;
        public required string List;
    }

    private static readonly string[] Lists = { "terms", "names", "books" };
    private string _lang = "en";
    private JsonObject _raw = new();
    private readonly List<Pair> _terms = new();
    private readonly ComboBox _picker = new() { Width = 220 };
    private readonly TextBlock _status = Ui.T("", Ui.Callout, Ui.Secondary, wrap: false);
    private readonly Button _save;
    private readonly TextBox _names = new() { AcceptsReturn = true, TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _enNote = Ui.T("Fixed translations into English come from each spoken language's list: choose that language above.", Ui.Callout, Ui.Secondary);
    private readonly StackPanel _termsBox = new() { Spacing = 8 };
    private readonly StackPanel _termsList = new() { Spacing = 8 };
    private bool _dirty, _loading, _loaded;

    public GlossaryTab(AppModel model) : base(model, "Glossary")
    {
        _picker.Items.Add(new ComboBoxItem { Content = OutputLanguage.English.Name, Tag = "en" });
        foreach (var l in OutputLanguage.All.Where(l => l.Available && l.Id != "en"))
            _picker.Items.Add(new ComboBoxItem { Content = l.Name, Tag = l.Id });
        _picker.SelectedIndex = 0;
        _picker.SelectionChanged += (_, _) =>
        {
            if (_picker.SelectedItem is ComboBoxItem { Tag: string code } && code != _lang) { _lang = code; _ = Load(); }
        };
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(_picker, "Language");
        _save = Ui.Button("Save", () => _ = Save());
        _save.IsEnabled = false;
        _save.KeyboardAccelerators.Add(new KeyboardAccelerator { Key = Windows.System.VirtualKey.S, Modifiers = Windows.System.VirtualKeyModifiers.Control });
        var top = Ui.Field(Ui.Row(10, Ui.T("Language"), _picker), Ui.Row(12, _status, _save));
        _status.VerticalAlignment = VerticalAlignment.Center;
        ((FrameworkElement)top.Children[0]).VerticalAlignment = VerticalAlignment.Center;
        Add(top);

        Add(Ui.T("Names of people in the congregation (one per line). Hear Church keeps them as they are.", Ui.Callout, Ui.Secondary));
        _names.TextChanged += (_, _) => Dirty();
        Add(_names);
        Add(_enNote);

        var plus = new Button { Content = Ui.Icon("", 12) };
        Ui.Tip(plus, "Add a word");
        plus.Click += (_, _) => { AddPair("", "", "terms"); Dirty(); };
        _termsBox.Children.Add(Ui.Field(Ui.T("Words with a fixed translation", Ui.Callout, Ui.Secondary), plus));
        _termsBox.Children.Add(new Border
        {
            Background = Ui.Card,
            BorderBrush = Ui.Res("CardStrokeColorDefaultBrush"),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(12),
            MinHeight = 60,
            Child = _termsList,
        });
        Add(_termsBox);
        ShowLanguage();
        _ = Load();
    }

    /// <summary>Shows another language's glossary, as picked above.</summary>
    public void Pick(string code) =>
        _picker.SelectedItem = _picker.Items.OfType<ComboBoxItem>().FirstOrDefault(i => (string)i.Tag == code) ?? _picker.SelectedItem;

    public override void Refresh()
    {
        base.Refresh();
        if (!_loaded && !_loading && Model.Engine.State == Engine.RunState.Running) _ = Load();
    }

    private void Dirty()
    {
        if (_loading) return;
        _dirty = true;
        _save.IsEnabled = true;
    }

    private void ShowLanguage()
    {
        var en = _lang == "en";
        _names.Height = en ? 200 : 90;
        _enNote.Visibility = en ? Visibility.Visible : Visibility.Collapsed;
        _termsBox.Visibility = en ? Visibility.Collapsed : Visibility.Visible;
    }

    private void AddPair(string en, string to, string list)
    {
        var pair = new Pair
        {
            En = new TextBox { Text = en, PlaceholderText = "English" },
            To = new TextBox { Text = to, PlaceholderText = OutputLanguage.NameOf(_lang) },
            List = list,
        };
        pair.En.TextChanged += (_, _) => Dirty();
        pair.To.TextChanged += (_, _) => Dirty();
        var minus = new Button { Content = Ui.Icon("", 12) };
        Ui.Tip(minus, "Remove");
        var row = new Grid { ColumnSpacing = 8 };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var arrow = Ui.Icon("", 12, Ui.Secondary);
        arrow.VerticalAlignment = VerticalAlignment.Center;
        Grid.SetColumn(arrow, 1);
        Grid.SetColumn(pair.To, 2);
        Grid.SetColumn(minus, 3);
        row.Children.Add(pair.En);
        row.Children.Add(arrow);
        row.Children.Add(pair.To);
        row.Children.Add(minus);
        minus.Click += (_, _) => { _terms.Remove(pair); _termsList.Children.Remove(row); Dirty(); };
        _terms.Add(pair);
        _termsList.Children.Add(row);
    }

    private async Task Load()
    {
        var lang = _lang;
        _loading = true;
        ShowLanguage();
        var g = await Model.Engine.GetObject($"api/glossary?lang={lang}");
        if (lang != _lang) return;   // another language was picked meanwhile; its own load finishes
        if (g == null)
        {
            _status.Text = Model.Engine.State == Engine.RunState.Running ? "Could not load the glossary." : "Waiting for the engine…";
            _loading = false;
            return;
        }
        _raw = g;
        _names.Text = string.Join("\r", Strings(g["local_names"]));
        _terms.Clear();
        _termsList.Children.Clear();
        if (lang != "en")
            foreach (var key in Lists)
                foreach (var item in (g[key] as JsonArray ?? new JsonArray()).OfType<JsonObject>())
                    AddPair(Str(item, "en"), Str(item, "fr"), key);
        _status.Text = "";
        _dirty = false;
        _save.IsEnabled = false;
        _loaded = true;
        _loading = false;
    }

    private async Task Save()
    {
        if (!_dirty) return;
        var g = (JsonObject)_raw.DeepClone();   // keeps anything this editor doesn't show
        g["local_names"] = new JsonArray(_names.Text.Split('\r', '\n').Select(n => n.Trim()).Where(n => n.Length > 0).Select(n => (JsonNode)n!).ToArray());
        var kept = _terms.Where(p => p.En.Text.Trim().Length > 0 && p.To.Text.Trim().Length > 0).ToList();
        if (_lang != "en")   // English shows (and changes) the names only
            foreach (var key in Lists)
            {
                var old = (_raw[key] as JsonArray ?? new JsonArray()).OfType<JsonObject>().ToList();
                g[key] = new JsonArray(kept.Where(p => p.List == key).Select(p =>
                {
                    var item = old.FirstOrDefault(o => Str(o, "en") == p.En.Text)?.DeepClone() as JsonObject ?? new JsonObject();   // keep notes etc.
                    item["en"] = p.En.Text;
                    item["fr"] = p.To.Text;
                    return (JsonNode)item;
                }).ToArray());
            }
        var r = await Model.Engine.Post($"api/glossary?lang={_lang}", g);
        if (r.Ok)
        {
            _raw = g;
            _dirty = false;
            _save.IsEnabled = false;
            _status.Text = "Saved";
        }
        else _status.Text = r.Message.Length == 0 ? "Could not save." : r.Message;
    }

    private static IEnumerable<string> Strings(JsonNode? n) =>
        (n as JsonArray ?? new JsonArray()).Select(x => x is JsonValue v && v.TryGetValue<string>(out var s) ? s : "").Where(s => s.Length > 0);

    private static string Str(JsonObject o, string key) =>
        o[key] is JsonValue v && v.TryGetValue<string>(out var s) ? s : "";
}
