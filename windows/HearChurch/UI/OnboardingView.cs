using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;

namespace HearChurch.UI;

/// <summary>First launch (the Mac's OnboardingView.swift). The congregation is made and managed on hearchurch.com: this PC
/// only signs in, with a code e-mailed to the congregation's addresses or one from "Add device" on another computer.
/// Then it picks its languages (which start downloading) and the input, and opens the main window. A congregation on
/// its own API key also enters the key.</summary>
public sealed class OnboardingView : Grid
{
    public enum Step { Welcome, Languages, Microphone, Claude }

    private readonly AppModel _model;
    private Step _step;
    private bool _ownKey = OnboardingProgress.OwnKey;
    private readonly Border _content = new();
    private readonly Border _footer = new() { Padding = new Thickness(16) };
    private readonly Slot _failed;
    private LanguageSelector? _selector;
    private Slot? _speechRow, _micSound;
    private LevelMeter? _meter;
    private SignInStep? _signIn;
    private Slot? _downloads;

    private List<Step> Order => _ownKey ? new() { Step.Welcome, Step.Languages, Step.Microphone, Step.Claude }
                                        : new() { Step.Welcome, Step.Languages, Step.Microphone };

    public OnboardingView(AppModel model)
    {
        _model = model;
        Background = Ui.WindowBackground;
        var s = Enum.TryParse<Step>(OnboardingProgress.Step, out var saved) ? saved : Step.Welcome;
        if (Credentials.Get(Credentials.Item.DeviceToken) is { Length: > 0 }) { if (s == Step.Welcome) s = Step.Languages; }
        else s = Step.Welcome;
        _step = s;

        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        _downloads = new Slot(() => string.Join(";", _model.Downloads.Select(p => $"{p.Id}:{p.State}:{(int)p.DoneMb}")), () => MainView.DownloadBar(_model));
        Children.Add(_downloads);
        _failed = new Slot(() => _model.Engine.State == Engine.RunState.Failed ? _model.Engine.FailReason : "", () => MainView.EngineFailedBanner(_model, XamlRoot));
        SetRow(_failed, 1);
        Children.Add(_failed);
        var scroll = new ScrollViewer { Content = _content, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        _content.MaxWidth = 620;
        _content.Padding = new Thickness(32);
        _content.HorizontalAlignment = HorizontalAlignment.Center;
        SetRow(scroll, 2);
        Children.Add(scroll);
        var d = Ui.Divider();
        SetRow(d, 3);
        Children.Add(d);
        SetRow(_footer, 4);
        Children.Add(_footer);
        _model.Mic.LevelChanged += MicLevel;
        Unloaded += (_, _) => { _model.Mic.LevelChanged -= MicLevel; if (_step == Step.Microphone) _model.MeterDisappeared(); };
        Show(_step);
    }

    public Step Current => _step;

    private void MicLevel() => _meter?.SetDb(_model.Mic.Db);

    public void Refresh()
    {
        _downloads?.Refresh();
        _failed.Refresh();
        _selector?.Refresh();
        _speechRow?.Refresh();
        _micSound?.Refresh();
        _signIn?.RefreshFooter();
        if (_step != Step.Welcome) UpdateFooterButtons();
    }

    private void Show(Step step)
    {
        if (_step == Step.Microphone && step != Step.Microphone) _model.MeterDisappeared();
        if (step == Step.Microphone && (_step != Step.Microphone || _meter == null)) _model.MeterAppeared();
        _step = step;
        OnboardingProgress.Step = step.ToString();
        _selector = null; _speechRow = null; _micSound = null; _meter = null; _signIn = null;
        _error = null;
        var p = new StackPanel { Spacing = 16 };
        switch (step)
        {
            case Step.Welcome:
                _signIn = new SignInStep(_model, own =>
                {
                    _ownKey = own;
                    OnboardingProgress.OwnKey = own;
                    Show(Step.Languages);
                });
                p.Children.Add(_signIn);
                break;
            case Step.Languages:
                p.Children.Add(StepTitle("Languages", "Choose the languages people speak at the microphone, and the languages listeners can choose. Every language you choose downloads now: keep going while it does. You can add or remove languages later in Settings."));
                _selector = new LanguageSelector(_model, showTitle: false, everyLanguage: true);
                p.Children.Add(_selector);
                p.Children.Add(Ui.Divider());
                _speechRow = new Slot(() => _model.Package("speech")?.Key ?? "", SpeechPackageRow);
                _speechRow.Refresh();
                p.Children.Add(_speechRow);
                break;
            case Step.Microphone:
                p.Children.Add(StepTitle("Microphone", "Hear Church listens through this PC's microphone, or a cable from the sound system (the hearing-assist receiver's line-in or USB input). The sound itself never leaves this PC: only the words go out, as text, to be translated."));
                if (WinSystem.MicrophoneBlocked)
                {
                    p.Children.Add(Ui.Label("The microphone is blocked for desktop apps.", "", Ui.Red));
                    p.Children.Add(Ui.Button("Open Settings → Microphone", () => AppModel.Open("ms-settings:privacy-microphone")));
                    p.Children.Add(Ui.T("Turn on \"Let desktop apps access your microphone\" there, then come back.", Ui.Callout, Ui.Secondary));
                }
                else
                {
                    _micSound = new Slot(() => _model.Engine.Settings?.Key ?? "", () =>
                        _model.Engine.Settings is { } st ? MainView.InputPicker(_model, st) : new ProgressRing { IsActive = true, Width = 16, Height = 16, HorizontalAlignment = HorizontalAlignment.Left });
                    _micSound.Refresh();
                    p.Children.Add(_micSound);
                    p.Children.Add(Ui.T("Say something to check the level:", color: Ui.Secondary));
                    _meter = new LevelMeter(400) { HorizontalAlignment = HorizontalAlignment.Left };
                    p.Children.Add(_meter);
                }
                p.Children.Add(Ui.Divider());
                var share = new CheckBox
                {
                    IsChecked = _model.Profile.ShareLogs,
                    Content = Ui.Column(2, Ui.T("Share diagnostic logs with Hear Church support"),
                                        Ui.T(DiagnosticLogText.Detail + " You can turn this off in Settings.", Ui.Callout, Ui.Secondary)),
                };
                share.Click += (_, _) => _model.SetShareLogs(share.IsChecked == true);
                p.Children.Add(share);
                break;
            case Step.Claude:
                p.Children.Add(StepTitle("Your translation key", "Your congregation translates with its own API key, and is billed directly for what it uses. The key is kept in Windows Credential Manager on this PC."));
                var key = new PasswordBox { PlaceholderText = "sk-ant-…", Width = 380 };
                var status = new StackPanel();
                var save = Ui.Button("Test and save", async () =>
                {
                    status.Children.Clear();
                    status.Children.Add(new ProgressRing { IsActive = true, Width = 16, Height = 16, HorizontalAlignment = HorizontalAlignment.Left });
                    var why = await _model.SaveKey(key.Password);
                    status.Children.Clear();
                    status.Children.Add(why == null ? Ui.Label("The key works.", "", Ui.Green) : Ui.T(why, color: Ui.Red));
                    UpdateFooterButtons();
                });
                p.Children.Add(Ui.Row(8, key, save));
                p.Children.Add(status);
                break;
        }
        _content.Child = p;
        BuildFooter();
    }

    private static UIElement StepTitle(string title, string subtitle) =>
        new StackPanel { Spacing = 6, Margin = new Thickness(0, 0, 0, 8), Children = { Ui.T(title, Ui.LargeTitle, bold: true), Ui.T(subtitle, color: Ui.Secondary) } };

    private UIElement SpeechPackageRow()
    {
        var g = new Grid();
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        g.Children.Add(Ui.Row(8, Ui.T("Speech recognition", color: Ui.Secondary, wrap: false), Ui.T("needed for every language", Ui.Callout, Ui.Tertiary, wrap: false)));
        var badge = PackageBadge(_model.Package("speech"), OutputLanguage.SpeechDownloadMB);
        SetColumn((FrameworkElement)badge, 1);
        g.Children.Add(badge);
        return g;
    }

    public static UIElement PackageBadge(PackageStatus? p, double sizeMB) => p?.State switch
    {
        "ready" => Ui.Label("Ready", "", Ui.Green, Ui.Callout),
        "downloading" => Ui.Row(6, new ProgressBar { Minimum = 0, Maximum = 1, Width = 70, Value = Math.Min(1, p.DoneMb / Math.Max(1, p.SizeMb)), VerticalAlignment = VerticalAlignment.Center },
                                Ui.T(Format.Size(p.SizeMb), Ui.Callout, Ui.Secondary, wrap: false)),
        "failed" => Ui.Label("Download failed", "", Ui.Orange, Ui.Callout),
        _ => Ui.T(Format.Size(p?.SizeMb ?? sizeMB), Ui.Callout, Ui.Secondary, wrap: false),
    };

    // ---------------------------------------------------------------- footer

    private Button? _next;
    private TextBlock? _error;

    private void BuildFooter()
    {
        if (_step == Step.Welcome)
        {
            _footer.Child = _signIn!.Footer;
            return;
        }
        var g = new Grid { ColumnSpacing = 8 };
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        if (_step != Order[1]) g.Children.Add(Ui.Button("Back", () => Move(-1)));   // signed in: no way back to the sign-in
        _error = Ui.T("", Ui.Callout, Ui.Red);
        _error.VerticalAlignment = VerticalAlignment.Center;
        SetColumn(_error, 1);
        g.Children.Add(_error);
        _next = Ui.Button(ButtonTitle, Next, accent: true);
        SetColumn(_next, 2);
        g.Children.Add(_next);
        _footer.Child = g;
        UpdateFooterButtons();
    }

    private bool IsLast => _step == Order[^1];
    private string ButtonTitle => IsLast ? "Open Hear Church" : "Continue";

    private bool CanContinue => _step switch
    {
        Step.Languages => _model.Profile.Speakers.Count > 0 && _model.Profile.Targets.Count > 0,
        Step.Claude => _model.KeyState == AppModel.KeyStates.Ok,
        _ => true,
    };

    private void UpdateFooterButtons()
    {
        if (_next == null) return;
        _next.Content = ButtonTitle;
        _next.IsEnabled = CanContinue;
    }

    private void Move(int by)
    {
        var i = Order.IndexOf(_step);
        if (i < 0) return;
        _model.Profile.Save();
        Show(Order[Math.Max(1, Math.Min(Order.Count - 1, i + by))]);
    }

    /// <summary>Continue, or on the last step open the main window (and start with Windows, as the Mac does).</summary>
    public void Next()
    {
        if (!IsLast) { Move(1); return; }
        if (!LoginItem.Enabled) LoginItem.Set(true);   // turned off in Settings → General
        OnboardingProgress.Clear();
        _model.FinishSetup();
    }

    /// <summary>For the screenshots in CI: sign in with a pairing code, as typed into "Use a code from another computer".</summary>
    public Task SignInWithCode(string code) => _signIn?.SignInWithCode(code) ?? Task.CompletedTask;
    public void ChooseCodeWay() => _signIn?.Choose(SignInStep.Way.OtherComputer);
}

/// <summary>Signing in: a code e-mailed to every address of the congregation, or one from Add device on a signed-in
/// computer (POST /api/hc/email-code, then /api/hc/pair). A new church signs up on hearchurch.com/start first.</summary>
public sealed class SignInStep : StackPanel
{
    public enum Way { None, Email, OtherComputer }

    private readonly AppModel _model;
    private readonly Action<bool> _onSignedIn;
    private Way _way;
    private readonly Border _emailChoice, _codeChoice;
    private readonly StackPanel _emailPanel, _codePanel;
    private readonly TextBox _email = new() { PlaceholderText = "name@example.com", Width = 320 };
    private readonly Button _send;
    private readonly ProgressRing _sending = new() { Width = 16, Height = 16, IsActive = false };
    private readonly TextBlock _sent = Ui.T("", color: Ui.Secondary);
    private readonly TextBox _code = new() { PlaceholderText = "XXXX-XXXX", Width = 260, FontSize = 26, FontFamily = new FontFamily("Consolas") };
    private readonly Button _signInButton;
    private readonly ProgressRing _busy = new() { Width = 16, Height = 16, IsActive = false };
    private readonly TextBlock _problem = Ui.T("", color: Ui.Red, select: true);
    private readonly StackPanel _sentRow;
    private bool _codeSent;
    public UIElement Footer { get; }
    private readonly Slot _footerSlot;

    public SignInStep(AppModel model, Action<bool> onSignedIn)
    {
        _model = model;
        _onSignedIn = onSignedIn;
        Spacing = 16;
        Children.Add(new StackPanel
        {
            Spacing = 6, Margin = new Thickness(0, 0, 0, 8),
            Children =
            {
                Ui.T("Welcome to Hear Church", Ui.LargeTitle, bold: true),
                Ui.T("This PC listens to your meeting and translates it live. Members hear the translation and read captions on their own phones, through hearchurch.com.", color: Ui.Secondary),
            },
        });
        _emailChoice = Choice(Way.Email, "Sign in with your email", "",
            "Use an e-mail address of your congregation. We send a code to every address of the congregation.");
        _codeChoice = Choice(Way.OtherComputer, "Use a code from another computer", "",
            "On a Mac or PC already signed in, open Hear Church → Settings → Congregation → Add device.");
        Children.Add(_emailChoice);
        Children.Add(_codeChoice);

        _send = Ui.Button("Send code", () => _ = Send());
        _send.IsEnabled = false;
        _email.TextChanged += (_, _) => _send.IsEnabled = HubClient.IsEmail(_email.Text);
        _email.KeyDown += (_, e) => { if (e.Key == Windows.System.VirtualKey.Enter) _ = Send(); };
        _sentRow = Ui.Row(6, Ui.Icon("", 14, Ui.Secondary), _sent);
        _sentRow.Visibility = Visibility.Collapsed;
        _emailPanel = new StackPanel { Spacing = 12, Visibility = Visibility.Collapsed };
        _emailPanel.Children.Add(Ui.Row(8, _email, _send, _sending));
        _emailPanel.Children.Add(_sentRow);

        _signInButton = Ui.Button("Sign in", () => _ = SignIn(), accent: true);
        _signInButton.IsEnabled = false;
        _code.TextChanged += (_, _) =>
        {
            var shown = HubClient.PairingFormat(_code.Text);
            if (shown != _code.Text) { _code.Text = shown; _code.SelectionStart = shown.Length; }
            _signInButton.IsEnabled = CodeComplete;
        };
        _code.KeyDown += (_, e) => { if (e.Key == Windows.System.VirtualKey.Enter) _ = SignIn(); };
        _codePanel = new StackPanel { Spacing = 8, Visibility = Visibility.Collapsed };
        _codePanel.Children.Add(Ui.T("Enter the code. Codes work once, for 15 minutes.", Ui.Callout, Ui.Secondary));
        var codeRow = Ui.Row(8, _code, _signInButton, _busy);
        foreach (var c in codeRow.Children) ((FrameworkElement)c).VerticalAlignment = VerticalAlignment.Center;
        _codePanel.Children.Add(codeRow);

        Children.Add(_emailPanel);
        Children.Add(_codePanel);
        Children.Add(_problem);
        var start = new HyperlinkButton { NavigateUri = AppPaths.HubPath("start"), Padding = new Thickness(0),
                                          Content = Ui.Label("New church? Get started on hearchurch.com", "", Ui.Accent) };
        Children.Add(start);

        _footerSlot = new Slot(FooterKey, BuildFooter);
        _footerSlot.Refresh();
        Footer = _footerSlot;
    }

    private bool CodeComplete => _code.Text.Count(char.IsLetterOrDigit) == 8;

    private Border Choice(Way w, string title, string glyph, string detail)
    {
        var g = new Grid { ColumnSpacing = 14 };
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var icon = Ui.Icon(glyph, 28, Ui.Accent);
        icon.VerticalAlignment = VerticalAlignment.Center;
        g.Children.Add(icon);
        var text = Ui.Column(4, Ui.T(title, 18, semibold: true), Ui.T(detail, Ui.Callout, Ui.Secondary));
        Grid.SetColumn(text, 1);
        g.Children.Add(text);
        var mark = Ui.Icon("", 14, Ui.Tertiary);   // ChevronRight
        mark.VerticalAlignment = VerticalAlignment.Center;
        Grid.SetColumn(mark, 2);
        g.Children.Add(mark);
        var b = new Button
        {
            Content = g, HorizontalAlignment = HorizontalAlignment.Stretch, HorizontalContentAlignment = HorizontalAlignment.Stretch,
            Padding = new Thickness(16), Background = Ui.Card, CornerRadius = new CornerRadius(10),
        };
        b.Click += (_, _) => Choose(w);
        return new Border { Child = b, CornerRadius = new CornerRadius(10), BorderThickness = new Thickness(1.5), Tag = mark };
    }

    public void Choose(Way w)
    {
        if (_way == w) return;
        _way = w;
        _code.Text = "";
        _problem.Text = "";
        foreach (var (border, way) in new[] { (_emailChoice, Way.Email), (_codeChoice, Way.OtherComputer) })
        {
            var on = way == w;
            border.BorderBrush = on ? Ui.Accent : null;
            var mark = (FontIcon)border.Tag;
            mark.Glyph = on ? "" : "";
            mark.Foreground = on ? Ui.Accent : Ui.Tertiary;
        }
        _emailPanel.Visibility = w == Way.Email ? Visibility.Visible : Visibility.Collapsed;
        _codePanel.Visibility = w == Way.OtherComputer || (w == Way.Email && _codeSent) ? Visibility.Visible : Visibility.Collapsed;
        if (w == Way.Email) _email.Focus(FocusState.Programmatic);
        else _code.Focus(FocusState.Programmatic);
    }

    private async Task Send()
    {
        var e = _email.Text.Trim();
        if (!HubClient.IsEmail(e) || _sending.IsActive) return;
        _sending.IsActive = true;
        _send.IsEnabled = false;
        _problem.Text = "";
        try
        {
            _sent.Text = await HubClient.EmailCode(e);
            _sentRow.Visibility = Visibility.Visible;
            _codeSent = true;
            _send.Content = "Send again";
            _codePanel.Visibility = Visibility.Visible;
        }
        catch (Exception ex) { _problem.Text = ex.Message; }
        finally
        {
            _sending.IsActive = false;
            _send.IsEnabled = HubClient.IsEmail(_email.Text);
        }
    }

    public async Task SignInWithCode(string code)
    {
        Choose(Way.OtherComputer);
        _code.Text = HubClient.PairingFormat(code);
        await SignIn();
    }

    private async Task SignIn()
    {
        if (!CodeComplete || _busy.IsActive) return;
        _busy.IsActive = true;
        _signInButton.IsEnabled = false;
        _problem.Text = "";
        try
        {
            var j = await HubClient.Pair(_code.Text, AppModel.DeviceName);
            if (!Credentials.Set(Credentials.Item.DeviceToken, j.Token))
            {
                _problem.Text = "Could not save to Windows Credential Manager on this PC. Try again.";
                return;
            }
            _model.SignedIn(j);
            _code.Text = "";
            _onSignedIn(j.Billing == "own_key");
        }
        catch (Exception ex) { _problem.Text = ex.Message; }
        finally
        {
            _busy.IsActive = false;
            _signInButton.IsEnabled = CodeComplete;
        }
    }

    // The opening screen's footer: the voices line (with the download's progress while one runs), then the help links.
    public void RefreshFooter() => _footerSlot.Refresh();

    private string FooterKey() => string.Join(";", _model.Engine.Packages.Where(p => p.State == "downloading").Select(p => $"{p.Id}:{(int)p.DoneMb}"));

    private UIElement BuildFooter()
    {
        var active = _model.Engine.Packages.Where(p => p.State == "downloading").ToList();
        double done = active.Sum(p => p.DoneMb), total = active.Sum(p => p.SizeMb);
        var line = Ui.Row(8, Ui.Label("Voices download while you finish setting up", "", Ui.Secondary, Ui.Callout));
        if (active.Count > 0)
        {
            line.Children.Add(new ProgressBar { Minimum = 0, Maximum = 1, Width = 80, Value = total > 0 ? Math.Min(1, done / total) : 0, VerticalAlignment = VerticalAlignment.Center });
            line.Children.Add(Ui.T($"{Format.Size(done)} of {Format.Size(total)}", Ui.Callout, Ui.Secondary, wrap: false));
        }
        line.HorizontalAlignment = HorizontalAlignment.Center;
        var links = HelpLinks();
        links.HorizontalAlignment = HorizontalAlignment.Center;
        return Ui.Column(8, line, links);
    }

    /// <summary>Privacy, Terms and support (the opening screen's footer, and Settings).</summary>
    public static StackPanel HelpLinks()
    {
        var p = Ui.Row(16,
            Ui.Link("Privacy", "https://hearchurch.com/privacy"),
            Ui.Link("Terms", "https://hearchurch.com/terms"),
            Ui.Link("Voice credits", "https://hearchurch.com/credits"),
            Ui.Link("Open-source notices", ShowNotices),
            Ui.Link("Contact support", "mailto:support@hearchurch.com"));
        foreach (var c in p.Children) ((HyperlinkButton)c).FontSize = Ui.Callout;
        return p;
    }

    /// <summary>THIRD_PARTY_NOTICES.md shipped with the engine, in Explorer; the source page when it isn't there.</summary>
    public static void ShowNotices()
    {
        var engine = Engine.EnginePath();
        var notices = engine == null ? null : Path.Combine(Path.GetDirectoryName(engine)!, "_internal", "THIRD_PARTY_NOTICES.md");
        if (notices != null && File.Exists(notices))
        {
            try { System.Diagnostics.Process.Start("explorer.exe", $"/select,\"{notices}\""); } catch { }
        }
        else AppModel.Open("https://hearchurch.com/source");
    }
}
