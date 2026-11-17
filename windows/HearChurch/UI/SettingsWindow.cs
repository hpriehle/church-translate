using HearChurch.Core;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace HearChurch.UI;

/// <summary>Settings: only General for now (W5 brings the Mac's other tabs: Languages, Speaking, Glossary,
/// Congregation, Billing, AI).</summary>
public sealed class SettingsWindow : Window
{
    public Grid Root { get; }

    public SettingsWindow(AppModel model)
    {
        Title = "Hear Church Settings";
        AppWindow.Resize(new Windows.Graphics.SizeInt32(720, 560));
        AppWindow.SetIcon(Path.Combine(AppContext.BaseDirectory, "Assets", "HearChurch.ico"));

        var nav = new NavigationView
        {
            PaneDisplayMode = NavigationViewPaneDisplayMode.Left,
            IsBackButtonVisible = NavigationViewBackButtonVisible.Collapsed,
            IsSettingsVisible = false,
            IsPaneToggleButtonVisible = false,
            OpenPaneLength = 200,
        };
        var general = new NavigationViewItem { Content = "General", Icon = new FontIcon { Glyph = "" }, IsSelected = true };
        nav.MenuItems.Add(general);
        nav.SelectedItem = general;

        var p = new StackPanel { Spacing = 20, Padding = new Thickness(32, 24, 32, 32), MaxWidth = 640, HorizontalAlignment = HorizontalAlignment.Left };
        p.Children.Add(Ui.T("General", Ui.LargeTitle, bold: true));

        var atLogin = new ToggleSwitch { Header = "Open Hear Church when this PC starts", IsOn = LoginItem.Enabled };
        atLogin.Toggled += (_, _) => LoginItem.Set(atLogin.IsOn);
        p.Children.Add(Ui.Section("This PC", atLogin));

        var share = new CheckBox
        {
            IsChecked = model.Profile.ShareLogs,
            Content = Ui.Column(2, Ui.T("Share diagnostic logs with Hear Church support"), Ui.T(DiagnosticLogText.Detail, Ui.Callout, Ui.Secondary)),
        };
        share.Click += (_, _) => model.SetShareLogs(share.IsChecked == true);
        p.Children.Add(Ui.Section("About and help", share, Ui.Button("Show the engine's log", MainView.ShowLog), SignInStep.HelpLinks(),
            Ui.T($"Hear Church {Version} for Windows (test build)", Ui.Callout, Ui.Secondary)));

        if (model.Profile.SetupDone)
        {
            var who = model.Profile.Name.Length > 0 ? model.Profile.Name : "your congregation";
            p.Children.Add(Ui.Section("Congregation",
                Ui.T($"This PC is signed in to {who}.", Ui.Body),
                Ui.Button("Sign out of this PC", model.SignOut)));
        }
        p.Children.Add(Ui.T("Languages, voices, glossary, congregation and billing settings are not in the Windows app yet: use the Mac app, or hearchurch.com/account.", Ui.Callout, Ui.Secondary));

        nav.Content = new ScrollViewer { Content = p };
        Root = new Grid { Background = Ui.WindowBackground };
        Root.Children.Add(nav);
        Content = Root;
    }

    public static string Version
    {
        get
        {
            var v = typeof(SettingsWindow).Assembly.GetName().Version;
            return v == null ? "?" : $"{v.Major}.{v.Minor}.{v.Build}";
        }
    }
}
