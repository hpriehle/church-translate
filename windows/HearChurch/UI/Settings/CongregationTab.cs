using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace HearChurch.UI.Settings;

/// <summary>The congregation: its details, the listener link, its e-mails and computers ("Add device"), and signing
/// this PC out.</summary>
public sealed class CongregationTab : SettingsTab
{
    private HubClient.Account? _account;
    private string? _problem;
    private bool _loading, _busy;
    private string _newEmail = "";
    private string? _code;
    private DateTime _expires;
    private TextBlock? _codeLine;
    private readonly DispatcherTimer _tick = new() { Interval = TimeSpan.FromSeconds(1) };
    // the listener link
    private bool _editing, _saving, _saved;
    private string _slug = "";
    private string? _linkProblem;

    public CongregationTab(AppModel model) : base(model, "Congregation")
    {
        var p = model.Profile;
        Add(() => $"{p.Name}|{p.Stake}|{p.Address}", () => Ui.Group("Congregation", null,
            Ui.Field("Name", p.Name),
            p.Stake.Length > 0 ? Ui.Field("Stake", p.Stake) : null,
            Ui.Field("Address", p.Address),
            Ui.Field("Server", AppPaths.HubHost)));
        Add(() => p.SetupDone && p.Slug.Length > 0 ? $"{p.Slug}|{_editing}|{_saving}|{_saved}|{_linkProblem}|{Model.Running}" : "",
            () => p.SetupDone && p.Slug.Length > 0 ? ListenerLink() : null);
        Add(() => p.SetupDone ? $"{EmailsKey()}|{_busy}|{_problem == null}" : "", () => p.SetupDone ? Emails() : null);
        Add(() => p.SetupDone ? $"{DevicesKey()}|{_busy}|{_code}" : "", () => p.SetupDone ? Devices() : null);
        Add(() => $"{_problem}|{_loading && _account == null}", () =>
        {
            if (_problem != null)
                return Ui.T(_problem, Ui.Body, _problem == new HubClient.NotAvailable().Message ? Ui.Secondary : Ui.Red, select: true);
            return _loading && _account == null ? Ui.Busy() : null;
        });
        Add(() => $"{Model.Running}", () =>
        {
            var b = Ui.Button("Sign this PC out…", () => _ = SignOut());
            b.Foreground = Ui.Red;
            b.IsEnabled = !Model.Running;
            return Ui.Group(null, null, b);
        });
        _tick.Tick += (_, _) => UpdateCode();
        _ = Load();
    }

    public override void Leaving() => _tick.Stop();

    private async Task Load()
    {
        if (!Model.Profile.SetupDone || AppModel.DeviceToken is not { } token) return;
        _loading = true;
        Refresh();
        try
        {
            _account = await HubClient.GetAccount(token);
            _problem = null;
        }
        catch (HubClient.SignedOut) { Model.SignOut(); }
        catch (Exception e) { _problem = e.Message; }
        _loading = false;
        Refresh();
    }

    /// <summary>Runs one account change against the hub; a revoked PC is signed out.</summary>
    private async Task<bool> Change(Func<string, Task<HubClient.Account>> call)
    {
        if (AppModel.DeviceToken is not { } token) { _problem = "This PC isn't connected to a congregation."; Refresh(); return false; }
        _busy = true;
        Refresh();
        try
        {
            _account = await call(token);
            _problem = null;
            return true;
        }
        catch (HubClient.SignedOut) { Model.SignOut(); return false; }
        catch (Exception e) { _problem = e.Message; return false; }
        finally { _busy = false; Refresh(); }
    }

    private async Task SignOut()
    {
        if (await Ui.Confirm(XamlRoot, $"Sign this PC out of {Model.Profile.Name}?",
                "It stops publishing meetings until it is connected again (with a code from Add device on another computer, or e-mailed). The API key stays.",
                "Sign Out"))
            Model.SignOut();
    }

    // ---------------------------------------------------------------- e-mails

    private string EmailsKey() => _account == null ? "-" : string.Join(",", _account.Emails.Select(e => $"{e.Address}:{e.Main}"));

    /// <summary>The congregation's e-mails: the main one (billing receipts) first. Add, remove, make main.</summary>
    private UIElement Emails()
    {
        var rows = new List<UIElement>();
        if (_account is { } a)
        {
            foreach (var e in a.Emails)
            {
                var left = Ui.Row(8, Ui.T(e.Address, select: true));
                if (e.Main)
                    left.Children.Add(new Border
                    {
                        Background = Ui.Res("AccentFillColorSecondaryBrush"), CornerRadius = new CornerRadius(8), Padding = new Thickness(6, 1, 6, 1),
                        Child = Ui.T("Main", Ui.Caption, Ui.Res("TextOnAccentFillColorPrimaryBrush"), bold: true),
                        VerticalAlignment = VerticalAlignment.Center,
                    });
                UIElement? right = null;
                if (!e.Main)
                {
                    var main = Ui.Button("Make main", () => _ = Change(t => HubClient.SetMainEmail(e.Address, t)));
                    var remove = new Button { Content = Ui.Icon("", 12) };
                    remove.Click += (_, _) => _ = Change(t => HubClient.RemoveEmail(e.Address, t));
                    Ui.Tip(remove, "Remove this e-mail");
                    main.IsEnabled = remove.IsEnabled = !_busy;
                    right = Ui.Row(8, main, remove);
                }
                rows.Add(Ui.Field(left, right));
            }
            if (a.Emails.Count < 10)
            {
                var box = new TextBox { PlaceholderText = "Add an e-mail", Text = _newEmail, MinWidth = 280 };
                var add = Ui.Button("Add", () => _ = AddEmail());
                add.IsEnabled = HubClient.IsEmail(_newEmail) && !_busy;
                box.TextChanged += (_, _) => { _newEmail = box.Text; add.IsEnabled = HubClient.IsEmail(_newEmail) && !_busy; };
                box.KeyDown += (_, k) => { if (k.Key == Windows.System.VirtualKey.Enter) _ = AddEmail(); };
                rows.Add(Ui.Field(box, add));
            }
        }
        else if (_problem == null) rows.Add(Ui.T("Loading…", Ui.Body, Ui.Secondary));
        return Ui.Group("E-mails",
            "The main e-mail gets the receipts for credit. Any of these addresses can get a code to connect another computer.",
            rows.ToArray());
    }

    private async Task AddEmail()
    {
        var e = _newEmail.Trim();
        if (!HubClient.IsEmail(e) || _busy) return;
        if (await Change(t => HubClient.AddEmail(e, t))) { _newEmail = ""; Rebuild(); }
    }

    // ---------------------------------------------------------------- devices

    private string DevicesKey() => _account == null ? "-" : string.Join(",", _account.Devices.Select(d => $"{d.Id}:{d.Name}:{d.LastSeen}:{d.AppVersion}"));

    /// <summary>The congregation's computers: this one, the others (revoke), and "Add device" for a new one.</summary>
    private UIElement Devices()
    {
        var rows = new List<UIElement>();
        foreach (var d in _account?.Devices ?? new())
        {
            var name = Ui.Row(8, Ui.T(d.Name, wrap: false));
            if (d.This) name.Children.Add(Ui.T("This PC", Ui.Caption, Ui.Secondary, wrap: false));
            var left = Ui.Row(12, Ui.Icon("", 18, Ui.Secondary), Ui.Column(2, name, Ui.T(Detail(d), Ui.Caption, Ui.Secondary)));
            UIElement? right = null;
            if (!d.This)
            {
                var revoke = Ui.Button("Revoke…", () => _ = Revoke(d));
                revoke.IsEnabled = !_busy;
                right = revoke;
            }
            rows.Add(Ui.Field(left, right));
        }
        _codeLine = null;
        if (_code != null)
        {
            var code = Ui.T(_code, 40, bold: true, select: true);
            code.FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Consolas");
            code.HorizontalAlignment = HorizontalAlignment.Center;
            _codeLine = Ui.T("", Ui.Callout, Ui.Secondary);
            rows.Add(Ui.Column(6, code, _codeLine));
            UpdateCode();
            _tick.Start();
        }
        var add = Ui.Button(_code == null ? "Add device" : "New code", () => _ = AddDevice());
        add.IsEnabled = !_busy;
        rows.Add(_busy ? Ui.Row(8, add, Ui.Busy()) : (UIElement)add);
        return Ui.Group("Devices",
            "Add device shows a code to connect another computer to this congregation. A revoked computer stops publishing meetings at once.",
            rows.ToArray());
    }

    private void UpdateCode()
    {
        if (_codeLine == null) { _tick.Stop(); return; }
        var left = (int)(_expires - DateTime.UtcNow).TotalSeconds;
        if (left > 0)
            _codeLine.Text = $"On the new computer: Use a code from another computer. Works once, for {left / 60}:{left % 60:00} more.";
        else
        {
            _codeLine.Text = "That code has expired.";
            _tick.Stop();
        }
    }

    private static string Detail(HubClient.Device d)
    {
        var parts = new List<string>();
        if (d.AppVersion.Length > 0) parts.Add($"Hear Church {d.AppVersion}");
        if (d.LastSeen is { } seen) parts.Add("last seen " + Relative(seen));
        else if (d.CreatedAt is { } added) parts.Add("added " + added.ToString("MMM d, yyyy", System.Globalization.CultureInfo.InvariantCulture));
        return string.Join(" · ", parts);
    }

    /// <summary>"5 minutes ago", "yesterday" (the Mac's relative date, named).</summary>
    private static string Relative(DateTime t)
    {
        var s = (DateTime.Now - t).TotalSeconds;
        if (s < 60) return "now";
        if (s < 3600) return Plural((int)(s / 60), "minute") + " ago";
        if (s < 86400) return Plural((int)(s / 3600), "hour") + " ago";
        var days = (DateTime.Now.Date - t.Date).Days;
        if (days <= 1) return "yesterday";
        if (days < 7) return $"{days} days ago";
        if (days < 31) return Plural(days / 7, "week") + " ago";
        return days < 365 ? Plural(days / 30, "month") + " ago" : Plural(days / 365, "year") + " ago";
    }

    private static string Plural(int n, string what) => n == 1 ? $"1 {what}" : $"{n} {what}s";

    private async Task Revoke(HubClient.Device d)
    {
        if (await Ui.Confirm(XamlRoot, $"Revoke {d.Name}?",
                "It stops publishing meetings at once (a meeting it is running ends). It can connect again with a new code.", "Revoke"))
            await Change(t => HubClient.RevokeDevice(d.Id, t));
    }

    private async Task AddDevice()
    {
        if (AppModel.DeviceToken is not { } token) { _problem = "This PC isn't connected to a congregation."; Refresh(); return; }
        _busy = true;
        Refresh();
        try
        {
            (_code, _expires) = await HubClient.PairingCode(token);
            _problem = null;
        }
        catch (HubClient.SignedOut) { Model.SignOut(); }
        catch (Exception e) { _problem = e.Message; }
        _busy = false;
        Rebuild();
    }

    // ---------------------------------------------------------------- the listener link

    /// <summary>As typed → what the hub accepts: lowercase letters, digits, single dashes.</summary>
    private static string Cleaned(string slug)
    {
        var t = new string(slug.ToLowerInvariant().Select(c => c < 128 && char.IsLetterOrDigit(c) ? c : '-').ToArray());
        return string.Join("-", t.Split('-', StringSplitOptions.RemoveEmptyEntries));
    }

    /// <summary>The congregation's page, hearchurch.com/&lt;slug&gt;: open it, copy it, or change it. The old address (and
    /// every QR code printed with it) keeps working: the hub redirects it to the new one for good.</summary>
    private UIElement ListenerLink()
    {
        var host = AppPaths.HubHost;
        var rows = new List<UIElement>();
        if (_editing)
        {
            var box = new TextBox { PlaceholderText = "your-congregation", Text = _slug, MinWidth = 240 };
            var becomes = Ui.T("", Ui.Caption, Ui.Secondary);
            var save = Ui.Button("Save", () => _ = SaveSlug(), accent: true);
            void Update()
            {
                _slug = box.Text;
                var c = Cleaned(_slug);
                becomes.Text = _slug.Length > 0 && c != _slug ? $"Becomes {host}/{c}" : "";
                becomes.Visibility = becomes.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
                save.IsEnabled = c.Length >= 3 && c != Model.Profile.Slug && !_saving;
            }
            box.TextChanged += (_, _) => Update();
            box.KeyDown += (_, k) => { if (k.Key == Windows.System.VirtualKey.Enter) _ = SaveSlug(); };
            Update();
            rows.Add(Ui.Row(2, Ui.T($"{host}/", Ui.Body, Ui.Secondary), box));
            rows.Add(becomes);
            var buttons = Ui.Row(8, Ui.Button("Cancel", () => { _editing = false; _linkProblem = null; Refresh(); }), save);
            if (_saving) buttons.Children.Add(Ui.Busy());
            rows.Add(buttons);
            if (_linkProblem != null)
                rows.Add(Ui.T(_linkProblem, Ui.Body, _linkProblem == new HubClient.NotAvailable().Message ? Ui.Secondary : Ui.Red));
            foreach (var r in rows.OfType<FrameworkElement>()) r.VerticalAlignment = VerticalAlignment.Center;
        }
        else
        {
            var url = AppPaths.HubPath(Model.Profile.Slug).ToString();
            var change = Ui.Button("Change…", () => { _slug = Model.Profile.Slug; _linkProblem = null; _saved = false; _editing = true; Refresh(); });
            change.IsEnabled = !Model.Running;
            Ui.Tip(change, Model.Running ? "Change it after the meeting." : "Choose another address for your congregation's page");
            rows.Add(Ui.Field(Ui.T("Listener link"), Ui.Row(10, Ui.Link(url, url), change)));
            if (_saved) rows.Add(Ui.Label("Saved. The old link still works.", "", Ui.Secondary));
        }
        return Ui.Group("Listener link",
            "Listeners open this page or scan its QR code. After a change, the old link and printed QR codes keep working: they lead to the new one.",
            rows.ToArray());
    }

    private async Task SaveSlug()
    {
        var c = Cleaned(_slug);
        if (c.Length < 3 || c == Model.Profile.Slug || _saving) return;
        _saving = true;
        Refresh();
        _linkProblem = await Model.ChangeSlug(c);
        _saving = false;
        if (_linkProblem == null) { _editing = false; _saved = true; }
        Refresh();
    }
}
