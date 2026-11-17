using System.Text.Json.Nodes;
using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace HearChurch.UI.Settings;

// The only place in the app that names the AI provider: a congregation on its own key pastes an Anthropic API key,
// so the AI tab and its how-to say so. Everywhere else it is "the translator" (tests/test_no_brand_names.py).

/// <summary>Settings → AI, shown only when hearchurch.com says the congregation pays with its own key ("own_key").</summary>
public sealed class AITab : SettingsTab
{
    private string? _keyError;

    public AITab(AppModel model) : base(model, "AI")
    {
        var key = new PasswordBox { PlaceholderText = "Paste a new key (sk-ant-…)", MinWidth = 320 };
        var save = Ui.Button("Test and save", () => { });
        save.Click += async (_, _) =>
        {
            _keyError = await Model.SaveKey(key.Password);
            if (_keyError == null) key.Password = "";
            Refresh();
        };
        void Enable() => save.IsEnabled = key.Password.Length > 0 && !Model.Running;
        key.PasswordChanged += (_, _) => Enable();
        Enable();
        var guide = new Expander { Header = "How to get a key", Content = Guide(), HorizontalAlignment = HorizontalAlignment.Stretch, HorizontalContentAlignment = HorizontalAlignment.Left };

        var p = Ui.Group("Anthropic API key", "Your congregation translates with its own key, and Anthropic bills you directly for what you use.",
            new Slot(() => $"{Model.KeyState}|{Model.KeyProblem}", KeyState),
            Ui.Row(8, key, save),
            new Slot(() => _keyError ?? "", () => _keyError == null ? null : Ui.T(_keyError, Ui.Body, Ui.Red)),
            guide);
        Add(p);
        foreach (var s in FindSlots(p)) Track(s);
        Add(() => $"{Model.Engine.Settings?.Settings.Translate.ClaudeModel}|{Model.Running}", Translation);
        Model.Changed += Enable;
        Unloaded += (_, _) => Model.Changed -= Enable;
    }

    private readonly List<Slot> _inner = new();
    private void Track(Slot s) { _inner.Add(s); s.Refresh(); }

    private static IEnumerable<Slot> FindSlots(DependencyObject root)
    {
        if (root is Slot s) { yield return s; yield break; }
        var children = root switch
        {
            Panel p => p.Children.Cast<DependencyObject>(),
            Border b when b.Child != null => new[] { (DependencyObject)b.Child },
            _ => Enumerable.Empty<DependencyObject>(),
        };
        foreach (var c in children)
            foreach (var x in FindSlots(c)) yield return x;
    }

    public override void Refresh()
    {
        foreach (var s in _inner) s.Refresh();
        base.Refresh();
    }

    private UIElement? KeyState() => Model.KeyState switch
    {
        AppModel.KeyStates.Ok => Ui.Label("Saved in Windows Credential Manager and working.", "", Ui.Green),
        AppModel.KeyStates.Missing => Ui.T("No key yet."),
        AppModel.KeyStates.Bad => Ui.T(Model.KeyProblem, Ui.Body, Ui.Red),
        _ => Ui.Busy(),
    };

    /// <summary>How to make the key (the Mac's APIKeyGuide).</summary>
    private static UIElement Guide()
    {
        var p = Ui.Column(6,
            Ui.T("1. Sign in at console.anthropic.com (or create an account).", Ui.Callout),
            Ui.T("2. Billing: add a payment method and buy some credit. $10–25 is plenty to start.", Ui.Callout),
            Ui.T("3. Limits: set a monthly spend limit. $25 a month covers a few Sundays for most congregations.", Ui.Callout),
            Ui.T("4. API keys: Create Key, name it \"Hear Church\", and copy it.", Ui.Callout),
            Ui.T("5. Paste it above.", Ui.Callout),
            Ui.Link("Open the Anthropic console", "https://console.anthropic.com/settings/keys"));
        return p;
    }

    /// <summary>The translation model, by what it means to a church rather than by name.</summary>
    private static readonly (string Id, string Label)[] Choices = { ("claude-opus-5-5", "Best"), ("claude-sonnet-5-5", "Good, cheaper") };

    private static string QualityName(string id)
    {
        foreach (var c in Choices) if (c.Id == id) return c.Label;
        if (id.Contains("opus")) return "Best";
        if (id.Contains("sonnet")) return "Good";
        if (id.Contains("haiku")) return "Fast";
        return "Custom";
    }

    private UIElement? Translation()
    {
        if (Model.Engine.Settings is not { } s) return null;
        var current = s.Settings.Translate.ClaudeModel;
        var pick = new ComboBox { MinWidth = 200, IsEnabled = !Model.Running };
        foreach (var c in Choices) pick.Items.Add(new ComboBoxItem { Content = c.Label, Tag = c.Id });
        if (current.Length > 0 && !Choices.Any(c => c.Id == current)) pick.Items.Add(new ComboBoxItem { Content = QualityName(current), Tag = current });
        pick.SelectedItem = pick.Items.OfType<ComboBoxItem>().FirstOrDefault(i => (string)i.Tag == current);
        pick.SelectionChanged += (_, _) =>
        {
            if (pick.SelectedItem is ComboBoxItem { Tag: string m } && m != current)
                _ = Model.Engine.Set(new JsonObject { ["translate"] = new JsonObject { ["claude_model"] = m } });
        };
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(pick, "Quality");
        return Ui.Group("Translation", null, Ui.Field(Ui.T("Quality"), pick));
    }
}
