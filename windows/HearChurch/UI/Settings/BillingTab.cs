using System.Globalization;
using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace HearChurch.UI.Settings;

/// <summary>Settings → Billing (congregations on credit): what's left, the saved card, buying more (1-click with a card,
/// otherwise Stripe's payment page in the browser), automatic top-up, and what the meetings used. Everything is the
/// hub's (app/hear/api.py): this tab only shows it and calls its routes.</summary>
public sealed class BillingTab : SettingsTab
{
    private static readonly CultureInfo C = CultureInfo.InvariantCulture;
    private List<HubClient.Meeting> _meetings = new();
    private List<HubClient.Charge> _charges = new();
    private string? _historyNote, _problem, _note;
    private int? _buying;
    private bool _saveCard = true, _cardBusy, _opening, _autoBusy, _loaded;
    private CancellationTokenSource? _watching;

    public BillingTab(AppModel model) : base(model, "Billing")
    {
        Add(Key, Build);
        _ = Load();
    }

    public override void Leaving() => _watching?.Cancel();

    private string Key() =>
        $"{Model.Profile.SetupDone}|{Model.Billing?.Key}|{_meetings.Count}:{_meetings.FirstOrDefault()?.StartedAt}|{_charges.Count}|{_historyNote}|"
        + $"{_problem}|{_note}|{_buying}|{_saveCard}|{_cardBusy}|{_opening}|{_autoBusy}|{_loaded}";

    private UIElement Build()
    {
        var p = new StackPanel { Spacing = 22 };
        var b = Model.Billing;
        if (!Model.Profile.SetupDone)
            p.Children.Add(Ui.T("Connect this PC to a congregation first.", Ui.Body, Ui.Secondary));
        else if (b?.Included == true)
        {
            p.Children.Add(Ui.Group("Billing", null, Ui.T("Included: no charge", Ui.Title3)));   // "internal": no balance, nothing to buy
            History(p);
        }
        else
        {
            p.Children.Add(Balance(b));
            if (b != null)
            {
                p.Children.Add(Buy(b));
                if (b.CanSaveCard) { p.Children.Add(Card(b)); p.Children.Add(Auto(b)); }
            }
            p.Children.Add(Manage());
            History(p);
        }
        return p;
    }

    private UIElement Balance(HubClient.Billing? b)
    {
        if (b == null) return Ui.Group("Credit", null, Ui.T(_problem ?? "Loading…", Ui.Body, Ui.Secondary));
        var rows = new List<UIElement>();
        if (b.TrialText is { } trial)   // a new congregation's free trial: shown while it runs
            rows.Add(Ui.Label(trial, "", Ui.Accent, Ui.Title3));
        var big = Ui.Row(24,
            Ui.Column(2, Big(Hours(b.FreeLeftHours)), Ui.T("free this month", Ui.Body, Ui.Secondary)),
            Ui.Column(2, Big(Hours(Math.Max(0, b.PaidHoursLeft))), Ui.T("paid hours left", Ui.Body, Ui.Secondary)));
        rows.Add(b.PaidHoursLeft < 0 ? Ui.Field(big, Ui.T(Money.Usd(b.BalanceUsd), Ui.Title3, Ui.Red)) : (UIElement)big);
        rows.Add(Ui.T(FreeLine(b), Ui.Callout, Ui.Secondary));
        if (b.OutOfCredit) rows.Add(Ui.T(b.Message, Ui.Body, Ui.Red));
        return Ui.Group("Credit", null, rows.ToArray());

        static TextBlock Big(string s) => Ui.T(s, 34, semibold: true, wrap: false);
    }

    private static string FreeLine(HubClient.Billing b)
    {
        var s = "One language-hour free every month; unused free time doesn't carry over.";
        if (b.FreeResetsAt is { } r) s += $" Next free hour: {r.ToString("MMM d, yyyy", C)}.";
        return s + " An hour is one language for one hour of speech: a meeting translated into two languages uses two hours an hour. Listeners are unlimited.";
    }

    /// <summary>1-click with a saved card; otherwise Stripe's payment page (offering to save the card).</summary>
    private bool OneClick => Model.Billing is { Card: not null, CanSaveCard: true };

    private UIElement Buy(HubClient.Billing b)
    {
        var rows = new List<UIElement>();
        if (b.CanBuy)
        {
            if (b.DiscountPercent is { } pct) rows.Add(Ui.Label($"{pct}% off your next bundle (from your code)", "", Ui.Green));
            var grid = new Grid { ColumnSpacing = 8 };
            for (var i = 0; i < b.Bundles.Count; i++)
            {
                var x = b.Bundles[i];
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                var content = Ui.Column(1, Ui.T(Price(x.Usd, b.DiscountPercent), Ui.Body, bold: true, wrap: false),
                                         Ui.T($"{Hours(x.Hours)} hours", Ui.Caption, wrap: false),
                                         Ui.T($"${x.UsdPerHour.ToString("0.00", C)}/h", 11, Ui.Secondary, wrap: false));
                foreach (var t in content.Children.OfType<TextBlock>()) t.HorizontalAlignment = HorizontalAlignment.Center;
                var button = new Button { Content = content, HorizontalAlignment = HorizontalAlignment.Stretch, IsEnabled = _buying == null };
                button.Click += (_, _) => _ = OneClick ? ConfirmBuy(b, x) : BuyHours(x.Usd);
                Grid.SetColumn(button, i);
                grid.Children.Add(button);
            }
            rows.Add(grid);
            if (!OneClick && b.CanSaveCard)
            {
                var save = new CheckBox { Content = "Save this card for 1-click top-ups", IsChecked = _saveCard };
                save.Click += (_, _) => _saveCard = save.IsChecked == true;
                rows.Add(save);
            }
            if (_buying is { } usd)
                rows.Add(Ui.Row(8, Ui.Busy(), Ui.T(OneClick ? $"Charging your card for ${usd}…" : "Opening the payment page…", Ui.Callout, Ui.Secondary)));
            if (_note != null) rows.Add(Ui.T(_note, Ui.Callout, Ui.Secondary));
        }
        else rows.Add(Ui.T("Buying credit isn't available yet. Write to support@hearchurch.com.", Ui.Body, Ui.Secondary));
        if (_problem != null) rows.Add(Ui.T(_problem, Ui.Body, Ui.Red, select: true));
        return Ui.Group("Buy hours", OneClick
            ? $"One click charges your saved card ({b.Card?.Label}). Hours don't expire. Unused hours can be refunded on request. The receipt goes to the congregation's main e-mail."
            : "Hours don't expire. Unused hours can be refunded on request. A secure payment page opens in your browser; the receipt goes to the congregation's main e-mail.",
            rows.ToArray());
    }

    private UIElement Card(HubClient.Billing b)
    {
        UIElement row;
        if (b.Card is { } c)
        {
            var left = Ui.Row(10, Ui.Label(c.Label, ""));
            if (c.ExpMonth is { } m && c.ExpYear is { } y)
                left.Children.Add(Ui.T($"expires {m:00}/{y % 100:00}", Ui.Caption, Ui.Secondary, wrap: false));
            var replace = Ui.Button("Replace…", () => _ = AddCard());
            var remove = Ui.Button("Remove", () => _ = RemoveCard());
            remove.Foreground = Ui.Red;
            replace.IsEnabled = remove.IsEnabled = !_cardBusy;
            row = Ui.Field(left, Ui.Row(8, replace, remove));
        }
        else
        {
            var add = Ui.Button("Add a card…", () => _ = AddCard());
            add.IsEnabled = !_cardBusy;
            row = Ui.Field(Ui.T("No card saved", Ui.Body, Ui.Secondary), add);
        }
        return Ui.Group("Card", "The card is saved with Stripe, on Stripe's own page; Hear Church only sees its brand and last 4 digits.", row);
    }

    private UIElement Auto(HubClient.Billing b)
    {
        var a = b.AutoTopUp;
        var on = new ToggleSwitch { Header = "Top up automatically", IsOn = a.Enabled, IsEnabled = b.Card != null && !_autoBusy };
        on.Toggled += (_, _) => { if (on.IsOn != a.Enabled) _ = SetAuto(on.IsOn, a.Usd); };
        var pick = new ComboBox { MinWidth = 200, IsEnabled = b.Card != null && !_autoBusy };
        foreach (var x in b.Bundles) pick.Items.Add(new ComboBoxItem { Content = $"${x.Usd} · {Hours(x.Hours)} hours", Tag = x.Usd });
        pick.SelectedIndex = b.Bundles.FindIndex(x => x.Usd == a.Usd);
        pick.SelectionChanged += (_, _) => { if (pick.SelectedItem is ComboBoxItem { Tag: int usd } && usd != a.Usd) _ = SetAuto(a.Enabled, usd); };
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(pick, "Bundle");
        return Ui.Group("Automatic top-up", b.Card == null
                ? "Save a card to turn this on."
                : $"After a meeting, if fewer than {Hours(a.BelowHours)} paid hour is left, your card is charged for this bundle: never during a meeting, at most once a day. Each time, the main e-mail gets a note. If the card fails, automatic top-up turns off.",
            on, Ui.Field(Ui.T("Bundle"), pick));
    }

    /// <summary>Everything else (codes, receipts) is on the web account.</summary>
    private UIElement Manage()
    {
        var open = Ui.Button("Buy credit / Manage billing…", () => _ = OpenAccount());
        open.IsEnabled = !_opening;
        return Ui.Group(null, "Opens your account on hearchurch.com, already signed in: buy credit, change the card, see receipts, or redeem a code from Hear Church.",
            _opening ? Ui.Row(8, open, Ui.Busy()) : (UIElement)open);
    }

    private void History(StackPanel p)
    {
        var rows = new List<UIElement>();
        if (_historyNote != null) rows.Add(Ui.T(_historyNote, Ui.Body, Ui.Secondary));
        else if (_meetings.Count == 0) rows.Add(Ui.T(_loaded ? "No meetings yet." : "Loading…", Ui.Body, Ui.Secondary));
        foreach (var m in _meetings)
        {
            var right = Ui.Column(2, Ui.T($"{m.LanguageHours.ToString("0.00", C)} hours", wrap: false), Ui.T(Cost(m), Ui.Caption, Ui.Secondary, wrap: false));
            foreach (var t in right.Children.OfType<TextBlock>()) t.HorizontalAlignment = HorizontalAlignment.Right;
            rows.Add(Ui.Field(Ui.Column(2, Ui.T(m.StartedAt.ToString("MMM d, yyyy 'at' h:mm tt", C)),
                                         Ui.T(string.Join(", ", m.Languages.Select(OutputLanguage.NameOf)), Ui.Caption, Ui.Secondary)), right));
        }
        p.Children.Add(Ui.Group("Meetings", null, rows.ToArray()));
        if (_charges.Count == 0) return;
        p.Children.Add(Ui.Group("Payments", null, _charges.Select(c =>
        {
            var right = Ui.Row(12);
            if (c.Hours is { } h && c.Kind != "refund") right.Children.Add(Ui.T($"{Hours(h)} hours", Ui.Body, Ui.Secondary, wrap: false));
            right.Children.Add(Ui.T((c.AmountUsd > 0 ? "+" : "") + Money.Usd(c.AmountUsd), wrap: false));
            return (UIElement)Ui.Field(Ui.Row(10, Ui.T(Kind(c.Kind), wrap: false), Ui.T(c.At.ToString("MMM d, yyyy", C), Ui.Body, Ui.Secondary, wrap: false)), right);
        }).ToArray()));
    }

    private static string Cost(HubClient.Meeting m)
    {
        if (m.OwnKey) return "own key";
        if (m.Included) return "included";
        if (m.CostUsd > 0) return Money.Usd(m.CostUsd) + (m.FreeMinutes + m.TrialMinutes > 0 ? " (part free)" : "");
        return m.LanguageHours > 0 ? "free" : "no speech";
    }

    private static string Kind(string k) => k switch
    {
        "top_up" => "Credit bought",
        "manual_credit" => "Credit added",
        "free_hours" => "Free hours (code)",
        "refund" => "Refund",
        _ => k,
    };

    private static string Price(int usd, int? percent)
    {
        if (percent is not { } p) return $"${usd}";
        var v = usd * (100 - p) / 100.0;
        return v == Math.Round(v) ? $"${(int)v}" : "$" + v.ToString("0.00", C);
    }

    // ---------------------------------------------------------------- calls

    private async Task Load()
    {
        if (!Model.Profile.SetupDone || AppModel.DeviceToken is not { } token) { _loaded = true; Refresh(); return; }
        await Model.RefreshBilling();
        if (Model.Billing == null) _problem = $"Can't reach {AppPaths.HubHost} right now.";
        try
        {
            (_meetings, _charges) = await HubClient.Usage(token);
            _historyNote = null;
        }
        catch (HubClient.NotAvailable) { _historyNote = "The history is available after the next update."; }
        catch (HubClient.SignedOut) { Model.SignOut(); }
        catch (Exception e) { _historyNote = e.Message; }
        _loaded = true;
        Refresh();
    }

    /// <summary>"Buy 11 hours for $50 on Visa ••4242?": 1-click always asks first.</summary>
    private async Task ConfirmBuy(HubClient.Billing b, HubClient.Bundle x)
    {
        var price = Price(x.Usd, b.DiscountPercent);
        if (await Ui.Confirm(XamlRoot, $"Buy {Hours(x.Hours)} hours for {price} on {b.Card?.Short ?? "your card"}?", "", $"Buy for {price}"))
            await BuyHours(x.Usd);
    }

    private async Task BuyHours(int usd)
    {
        if (AppModel.DeviceToken is not { } token) return;
        var oneClick = OneClick;
        _buying = usd;
        _problem = null;
        _note = null;
        Refresh();
        try
        {
            if (oneClick)
            {
                var (paid, url) = await HubClient.TopUp(usd, Guid.NewGuid().ToString(), token);
                if (url != null)
                {
                    AppModel.Open(url);
                    _note = "Your bank wants to confirm this payment: finish it in your browser. The hours show here within a minute.";
                }
                else _note = paid;
                Watch();
                return;
            }
            AppModel.Open(await HubClient.Checkout(usd, _saveCard && Model.Billing?.CanSaveCard == true, token));
            _note = "Pay in your browser. The credit shows here within a minute.";
            Watch();
        }
        catch (HubClient.NeedsCheckout e)
        {
            // no card on file after all (removed on another computer), or the bank wants the full page
            _note = e.Message;
            try
            {
                AppModel.Open(await HubClient.Checkout(usd, true, token));
                Watch();
            }
            catch { }
        }
        catch (HubClient.SignedOut) { Model.SignOut(); }
        catch (Exception e) { _problem = e.Message; }
        finally
        {
            _buying = null;
            Refresh();
        }
    }

    private async Task AddCard()
    {
        if (AppModel.DeviceToken is not { } token) return;
        _cardBusy = true;
        Refresh();
        try
        {
            AppModel.Open(await HubClient.CardSetup(token));
            _note = "Enter the card on Stripe's page in your browser. It shows here within a minute.";
            _problem = null;
            Watch();
        }
        catch (HubClient.SignedOut) { Model.SignOut(); }
        catch (Exception e) { _problem = e.Message; }
        _cardBusy = false;
        Refresh();
    }

    private async Task RemoveCard()
    {
        if (AppModel.DeviceToken is not { } token) return;
        if (!await Ui.Confirm(XamlRoot, "Remove the saved card?", "Buying will open Stripe's payment page again, and automatic top-up turns off.", "Remove card"))
            return;
        _cardBusy = true;
        Refresh();
        try
        {
            await HubClient.RemoveCard(token);
            await Model.RefreshBilling();
        }
        catch (HubClient.SignedOut) { Model.SignOut(); }
        catch (Exception e) { _problem = e.Message; }
        _cardBusy = false;
        Refresh();
    }

    private async Task SetAuto(bool on, int usd)
    {
        if (AppModel.DeviceToken is not { } token) return;
        _autoBusy = true;
        Refresh();
        try
        {
            await HubClient.SetAutoTopUp(on, usd, token);
            await Model.RefreshBilling();
        }
        catch (HubClient.SignedOut) { Model.SignOut(); }
        catch (Exception e) { _problem = e.Message; }
        _autoBusy = false;
        Rebuild();   // a refused change puts the switch back
    }

    /// <summary>hearchurch.com/account, signed in by a one-time link from the hub (or the plain page, to sign in there).</summary>
    private async Task OpenAccount()
    {
        if (AppModel.DeviceToken is not { } token) return;
        _opening = true;
        Refresh();
        AppModel.Open(await HubClient.AccountLink(token));
        _opening = false;
        Refresh();
        Watch();   // a purchase or a code there shows here within a minute
    }

    /// <summary>After a payment or a card on Stripe's page: the webhook brings the change within seconds; look again for a while.</summary>
    private void Watch()
    {
        _watching?.Cancel();
        var cts = new CancellationTokenSource();
        _watching = cts;
        var before = Model.Billing?.Key;
        _ = Run();

        async Task Run()
        {
            foreach (var wait in new[] { 2, 3, 5, 5, 10, 15, 20, 30, 30 })
            {
                try { await Task.Delay(TimeSpan.FromSeconds(wait), cts.Token); } catch { return; }
                await Model.RefreshBilling();
                if (cts.IsCancellationRequested) return;
                if (Model.Billing?.Key != before)
                {
                    await Load();
                    return;
                }
            }
        }
    }
}
