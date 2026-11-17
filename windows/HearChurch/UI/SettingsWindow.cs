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
        var version = Ui.T(VersionLine(), Ui.Callout, Ui.Secondary);
        Button? check = null;
        check = Ui.Button("Check for Updates", async () =>
        {
            check!.IsEnabled = false;
            version.Text = "Checking for updates…";
            var answer = await App.Updates.Check();
            version.Text = VersionLine() + "\n" + answer;
            check.IsEnabled = true;
        });
        p.Children.Add(Ui.Section("About and help", share, Ui.Button("Show the engine's log", MainView.ShowLog), SignInStep.HelpLinks(),
            version, check));

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

    /// <summary>"Hear Church 0.4.0 (12) for Windows (test build)", plus the update channel when it isn't public.</summary>
    private static string VersionLine()
    {
        var u = App.Updates;
        var channel = u.Installed && Updates.Channel != "public" ? $", {Updates.Channel} updates" : "";
        var line = $"Hear Church {u.Version} for Windows (test build{channel})";
        return u.Ready != null ? $"{line}\nHear Church {u.Ready} is ready. It is installed the next time Hear Church quits." : line;
    }
}
