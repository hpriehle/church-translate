using HearChurch.Core;
using HearChurch.UI.Settings;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace HearChurch.UI;

/// <summary>Settings (the Mac's SettingsView.swift): General, Billing (or AI for a congregation on its own key),
/// Languages, Speaking, Glossary, Congregation, and About (the Mac's About panel and its "About and help").</summary>
public sealed class SettingsWindow : Window
{
    public const string General = "general", Billing = "billing", AI = "ai", Languages = "languages", Speaking = "voices",
                        Glossary = "glossary", Congregation = "congregation", About = "about";

    private readonly AppModel _model;
    private readonly NavigationView _nav;
    private SettingsTab? _tab;
    private string _shown = "";
    private bool _ownKey;

    public Grid Root { get; }
    public string Shown => _shown;
    /// <summary>The open tab (the screenshots in CI open a language, pick a glossary).</summary>
    public SettingsTab? Tab => _tab;

    public SettingsWindow(AppModel model)
    {
        _model = model;
        Title = "Hear Church Settings";
        AppWindow.Resize(new Windows.Graphics.SizeInt32(860, 680));
        AppWindow.SetIcon(Path.Combine(AppContext.BaseDirectory, "Assets", "HearChurch.ico"));

        _nav = new NavigationView
        {
            PaneDisplayMode = NavigationViewPaneDisplayMode.Left,
            IsBackButtonVisible = NavigationViewBackButtonVisible.Collapsed,
            IsSettingsVisible = false,
            IsPaneToggleButtonVisible = false,
            OpenPaneLength = 200,
        };
        _nav.SelectionChanged += (_, e) =>
        {
            if (e.SelectedItem is NavigationViewItem { Tag: string tag } && tag != _shown) Show(tag);
        };
        BuildMenu();

        Root = new Grid { Background = Ui.WindowBackground };
        Root.Children.Add(_nav);
        Content = Root;

        model.Changed += Changed;
        Closed += (_, _) =>
        {
            model.Changed -= Changed;
            _tab?.Leaving();
        };
        Select(Prefs.GetString(AppModel.SettingsTabKey) ?? General);
        _ = model.RefreshBilling();   // the operator may have switched it since
    }

    /// <summary>Credits: the Billing tab. Its own key (the hub says "own_key"): the AI tab instead, with the key.</summary>
    private void BuildMenu()
    {
        _ownKey = _model.OwnKeyBilling;
        _nav.MenuItems.Clear();
        void Add(string text, string glyph, string tag) =>
            _nav.MenuItems.Add(new NavigationViewItem { Content = text, Icon = new FontIcon { Glyph = glyph }, Tag = tag });
        Add("General", "", General);
        if (_ownKey) Add("AI", "", AI);
        else Add("Billing", "", Billing);
        Add("Languages", "", Languages);
        Add("Speaking", "", Speaking);
        Add("Glossary", "", Glossary);
        Add("Congregation", "", Congregation);
        Add("About", "", About);
    }

    /// <summary>Open a tab (the one chosen last, or one asked for: "Add or remove languages…"). A tab that isn't there
    /// for this congregation opens its counterpart.</summary>
    public void Select(string tag)
    {
        if (tag == AI && !_ownKey) tag = Billing;
        if (tag == Billing && _ownKey) tag = AI;
        var item = _nav.MenuItems.OfType<NavigationViewItem>().FirstOrDefault(i => (string)i.Tag == tag)
                   ?? (NavigationViewItem)_nav.MenuItems[0];
        _nav.SelectedItem = item;
        if ((string)item.Tag != _shown) Show((string)item.Tag);
    }

    private void Show(string tag)
    {
        _tab?.Leaving();
        _shown = tag;
        Prefs.Set(AppModel.SettingsTabKey, tag);
        _tab = tag switch
        {
            Billing => new BillingTab(_model),
            AI => new AITab(_model),
            Languages => new LanguagesTab(_model),
            Speaking => new SpeakingTab(_model),
            Glossary => new GlossaryTab(_model),
            Congregation => new CongregationTab(_model),
            About => new AboutTab(_model),
            _ => new GeneralTab(_model),
        };
        _nav.Content = _tab;
    }

    private void Changed()
    {
        if (_model.OwnKeyBilling != _ownKey)
        {
            var was = _shown;
            BuildMenu();
            _shown = "";
            Select(was);
            return;
        }
        _tab?.Refresh();
    }
}
