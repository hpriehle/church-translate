using System.Net.Http.Headers;
using System.Text;
using System.Text.Json.Nodes;

namespace HearChurch.Core;

/// <summary>hearchurch.com's API for the app (app/hear/api.py): sign-in by code, the account check, billing and the
/// API key check. The same routes and messages as the Mac's HubClient.swift.</summary>
public static class HubClient
{
    public sealed class Failure : Exception { public Failure(string message) : base(message) { } }
    /// <summary>The hub no longer knows this computer's token (revoked, or the congregation is gone): set up again.</summary>
    public sealed class SignedOut : Exception { public SignedOut() : base("This PC was disconnected from the congregation.") { } }

    public sealed record Joined(string Token, string Slug, string Name, string Church, string Billing,
                                string UnitType, string Stake, string Address);

    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(30) };
    /// <summary>Tells the hub this app knows the "internal" billing type.</summary>
    private const string BillingTypes = "credits,own_key,internal";

    /// <summary>"Connect by code": a code from "Add device" on another computer, or from the e-mail (POST /api/hc/pair).</summary>
    public static async Task<Joined> Pair(string code, string deviceName)
    {
        var (json, status) = await Send("/api/hc/pair", new JsonObject
        {
            ["code"] = code, ["device_name"] = deviceName, ["platform"] = AppModel.Platform,
        });
        if (status is >= 200 and < 300)
        {
            var cong = json["congregation"] as JsonObject ?? new JsonObject();
            var token = Str(json, "device_token");
            if (token.Length == 0) throw new Failure("The server did not send a device token. Try again.");
            return new Joined(token, Str(json, "slug"), Str(cong, "name"), Str(cong, "church"),
                              Str(json, "billing", "credits"), Str(cong, "unit_type"), Str(cong, "stake"), Str(cong, "address"));
        }
        if (status == 403 && Str(json, "error") == "bad_code")
            throw new Failure("That code is wrong or has expired. Ask for a new one: codes work once, for 15 minutes.");
        if (status == 429) throw TooManyTries(json);
        throw new Failure(Message(json) ?? $"Server error {status}.");
    }

    /// <summary>"Email me a code": the hub sends a code to every address of the congregation this e-mail belongs to.</summary>
    public static async Task<string> EmailCode(string email)
    {
        var (json, status) = await Send("/api/hc/email-code", new JsonObject { ["email"] = email });
        return status switch
        {
            >= 200 and < 300 => Str(json, "message", "If that e-mail belongs to a congregation, a code is on its way."),
            503 => throw new Failure("Hear Church can't send e-mail yet. On a computer that is already connected, open Settings → Congregation → Add device, and enter the code it shows."),
            422 => throw new Failure("That doesn't look like an e-mail address."),
            429 => throw new Failure("Too many codes asked for. Wait an hour, or use Add device on a connected computer."),
            _ => throw new Failure(Message(json) ?? $"Server error {status}."),
        };
    }

    /// <summary>Is this computer still part of its congregation? Only a clear 403 counts as signed out.</summary>
    public static async Task CheckAccount(string token)
    {
        var (json, status) = await Get("/api/hc/account", token);
        if (status == 403) throw new SignedOut();
        if (status is < 200 or >= 300 && !MissingRoute(json, status)) throw new Failure(Message(json) ?? $"Server error {status}.");
    }

    /// <summary>How the congregation pays: "credits", "own_key" or "internal".</summary>
    public static async Task<string> BillingMode(string token)
    {
        var (json, status) = await Get("/api/hc/billing", token);
        if (status == 403) throw new SignedOut();
        if (status is < 200 or >= 300) throw new Failure(Message(json) ?? $"Server error {status}.");
        return Str(json, "billing", "credits");
    }

    // ---------------------------------------------------------------- the congregation's account (Settings)

    /// <summary>The hub is older than this app and doesn't have that call yet. Nothing is wrong: show it quietly.</summary>
    public sealed class NotAvailable : Exception { public NotAvailable() : base("Available after the next update.") { } }

    public sealed record Email(string Address, bool Main);
    public sealed record Device(int Id, string Name, DateTime? CreatedAt, DateTime? LastSeen, string AppVersion, bool This);
    public sealed record Account(string MainEmail, List<Email> Emails, List<Device> Devices);

    public static async Task<Account> GetAccount(string token) => AccountAnswer(await Get("/api/hc/account", token));
    public static async Task<Account> AddEmail(string email, string token) =>
        AccountAnswer(await Send("/api/hc/emails", new JsonObject { ["email"] = email, ["main"] = false }, token));
    public static async Task<Account> SetMainEmail(string email, string token) =>
        AccountAnswer(await Send("/api/hc/emails/main", new JsonObject { ["email"] = email }, token));
    public static async Task<Account> RemoveEmail(string email, string token) =>
        AccountAnswer(await Send("/api/hc/emails/remove", new JsonObject { ["email"] = email }, token));
    public static async Task<Account> RevokeDevice(int id, string token) =>
        AccountAnswer(await Send("/api/hc/devices/revoke", new JsonObject { ["id"] = id }, token));

    /// <summary>"Add device": a one-time code another computer enters, and when it stops working.</summary>
    public static async Task<(string Code, DateTime Expires)> PairingCode(string token)
    {
        var (json, status) = await Send("/api/hc/pairing-code", new JsonObject(), token);
        if (MissingRoute(json, status)) throw new NotAvailable();
        if (status == 403) throw new SignedOut();
        if (status == 429) throw new Failure("Too many codes this hour. Try again later.");
        var code = Str(json, "code");
        if (status is < 200 or >= 300 || code.Length == 0) throw new Failure(Message(json) ?? $"Server error {status}.");
        return (code, DateTime.UtcNow.AddSeconds(Num(json, "expires_in") ?? 900));
    }

    private static Account AccountAnswer((JsonObject Json, int Status) answer)
    {
        var (json, status) = answer;
        if (MissingRoute(json, status)) throw new NotAvailable();
        if (status == 403) throw new SignedOut();
        if (status == 429) throw new Failure(Message(json) ?? "Too many tries. Try again later.");
        if (status == 422) throw new Failure("That doesn't look like an e-mail address.");
        if (status is < 200 or >= 300) throw new Failure(Message(json) ?? $"Server error {status}.");
        var emails = (json["emails"] as JsonArray ?? new JsonArray()).OfType<JsonObject>()
            .Where(e => Str(e, "email").Length > 0).Select(e => new Email(Str(e, "email"), Bool(e, "main"))).ToList();
        var devices = (json["devices"] as JsonArray ?? new JsonArray()).OfType<JsonObject>()
            .Where(d => Num(d, "id") != null)
            .Select(d => new Device((int)Num(d, "id")!.Value, Str(d, "name", "Computer"), Date(Num(d, "created_at")), Date(Num(d, "last_seen")),
                                    Str(d, "app_version"), Bool(d, "this"))).ToList();
        var main = Str(json, "main_email");
        if (main.Length == 0) main = emails.FirstOrDefault(e => e.Main)?.Address ?? "";
        return new Account(main, emails, devices);
    }

    // ---------------------------------------------------------------- billing (Settings → Billing) and the listener link

    /// <summary>A credit bundle: its price, the language-hours it gives, and what an hour comes to.</summary>
    public sealed record Bundle(int Usd, double Hours, double UsdPerHour);

    /// <summary>The congregation's saved card, as Stripe describes it (the card itself stays at Stripe).</summary>
    public sealed record Card(string Brand, string Last4, int? ExpMonth, int? ExpYear)
    {
        private string B => Brand.Length == 0 ? "Card" : Brand == "amex" ? "Amex" : char.ToUpperInvariant(Brand[0]) + Brand[1..];
        public string Label => Last4.Length == 0 ? B : $"{B} ••••{Last4}";
        /// <summary>"Visa ••4242": in the question before a 1-click purchase.</summary>
        public string Short => Last4.Length == 0 ? B : $"{B} ••{Last4}";
    }

    /// <summary>Automatic top-up: after a meeting, when the paid hours left are under BelowHours, buy the Usd bundle.</summary>
    public sealed record AutoTopUp(bool Enabled = false, int Usd = 20, double BelowHours = 1);

    /// <summary>GET /api/hc/billing: how the congregation pays and what it has left.</summary>
    public sealed record Billing(string Mode, double BalanceUsd, double FreeLeftHours, double PaidHoursLeft, DateTime? FreeResetsAt,
                                 bool OutOfCredit, bool CanBuy, List<Bundle> Bundles, string Message, Card? Card, AutoTopUp AutoTopUp,
                                 int? DiscountPercent, bool CanSaveCard, string? TrialText)
    {
        public bool OwnKey => Mode == "own_key";
        public bool Included => Mode == "internal";
        /// <summary>Changes when anything Settings → Billing shows changes.</summary>
        public string Key => $"{Mode}|{BalanceUsd}|{FreeLeftHours}|{PaidHoursLeft}|{FreeResetsAt}|{OutOfCredit}|{CanBuy}|"
            + string.Join(",", Bundles.Select(b => b.Usd)) + $"|{Message}|{Card}|{AutoTopUp}|{DiscountPercent}|{CanSaveCard}|{TrialText}";
    }

    public static async Task<Billing> GetBilling(string token)
    {
        var (json, status) = await Get("/api/hc/billing", token);
        Check(json, status);
        var b = json["balance"] as JsonObject ?? new JsonObject();
        var bundles = (json["bundles"] as JsonArray ?? new JsonArray()).OfType<JsonObject>()
            .Where(x => Num(x, "usd") != null && Num(x, "hours") is > 0)
            .Select(x => new Bundle((int)Num(x, "usd")!.Value, Num(x, "hours")!.Value, Num(x, "usd_per_hour") ?? Num(x, "usd")!.Value / Num(x, "hours")!.Value))
            .ToList();
        if (bundles.Count == 0)   // a hub from before the bundles: its amounts at its one rate
        {
            var rate = Num(json, "usd_per_language_hour") ?? 5;
            bundles = (json["buy_usd"] as JsonArray ?? new JsonArray()).Select(n => Num(n)).Where(n => n != null)
                .Select(n => new Bundle((int)n!.Value, n.Value / Math.Max(rate, 0.01), rate)).ToList();
        }
        var free = (Num(b, "free_left_min") ?? 0) / 60;
        Card? card = null;
        if (json["card"] is JsonObject c)
            card = new Card(Str(c, "brand"), Str(c, "last4"), (int?)Num(c, "exp_month"), (int?)Num(c, "exp_year"));
        var auto = new AutoTopUp();
        if (json["auto_topup"] is JsonObject a)
            auto = new AutoTopUp(Bool(a, "enabled"), (int)(Num(a, "usd") ?? 20), Num(a, "below_hours") ?? 1);
        int? discount = json["discount"] is JsonObject d && Num(d, "percent") is { } p ? (int)p : null;
        // A new congregation's free trial while it runs (a hub with chat/hc-trial; older hubs leave it out).
        var trial = json["free_trial"] is JsonObject t && Str(t, "text") is { Length: > 0 } tt ? tt : null;
        return new Billing(Str(json, "billing", "credits"), Num(json, "balance_usd") ?? 0, free,
                           Num(json, "paid_hours_left") ?? ((Num(json, "language_hours_left") ?? 0) - free),
                           Date(Num(json, "free_resets_at")), Bool(json, "out_of_credit"), Bool(json, "can_buy"), bundles,
                           Str(json, "message"), card, auto, discount, Bool(json, "can_save_card"), trial);
    }

    public sealed record Meeting(DateTime StartedAt, List<string> Languages, double LanguageHours, double FreeMinutes,
                                 double TrialMinutes, double CostUsd, bool OwnKey, bool Included);
    public sealed record Charge(string Kind, DateTime At, double AmountUsd, double? Hours);

    /// <summary>GET /api/hc/usage: meetings and their language-hours, and credit bought or refunded, newest first.</summary>
    public static async Task<(List<Meeting> Meetings, List<Charge> Charges)> Usage(string token)
    {
        var (json, status) = await Get("/api/hc/usage", token);
        Check(json, status);
        var meetings = (json["meetings"] as JsonArray ?? new JsonArray()).OfType<JsonObject>().Select(m => new Meeting(
            Date(Num(m, "started_at")) ?? DateTime.UnixEpoch, (m["languages"] as JsonArray ?? new JsonArray()).Select(x => x?.ToString() ?? "").ToList(),
            Num(m, "language_hours") ?? 0, Num(m, "free_minutes") ?? 0, Num(m, "trial_minutes") ?? 0, Num(m, "cost_usd") ?? 0,
            Str(m, "billing") == "own_key", Str(m, "billing") == "internal")).ToList();
        var charges = (json["charges"] as JsonArray ?? new JsonArray()).OfType<JsonObject>().Select(c => new Charge(
            Str(c, "kind"), Date(Num(c, "at")) ?? DateTime.UnixEpoch, Num(c, "amount_usd") ?? 0, Num(c, "hours"))).ToList();
        return (meetings, charges);
    }

    /// <summary>POST /api/hc/checkout: a Stripe Checkout page for the bundle at that price, opened in the browser.</summary>
    public static async Task<string> Checkout(int usd, bool saveCard, string token)
    {
        var body = new JsonObject { ["usd"] = usd };
        if (saveCard) body["save_card"] = true;
        var (json, status) = await Send("/api/hc/checkout", body, token);
        if (status == 503) throw new Failure("Buying credit isn't set up yet. Write to support@hearchurch.com.");
        Check(json, status);
        return WebUrl(json) ?? throw new Failure("The payment page didn't open. Try again.");
    }

    /// <summary>POST /api/hc/card/setup: Stripe's page to save a card (opened in the browser; the card never reaches us).</summary>
    public static async Task<string> CardSetup(string token)
    {
        var (json, status) = await Send("/api/hc/card/setup", new JsonObject(), token);
        if (status == 503) throw new Failure("Saving a card isn't set up yet. Write to support@hearchurch.com.");
        Check(json, status);
        return WebUrl(json) ?? throw new Failure("Stripe's page didn't open. Try again.");
    }

    /// <summary>POST /api/hc/card/remove: forget the saved card (automatic top-up goes off with it).</summary>
    public static async Task RemoveCard(string token)
    {
        var (json, status) = await Send("/api/hc/card/remove", new JsonObject(), token);
        Check(json, status);
    }

    /// <summary>No saved card (or the bank wants the full payment page): use Checkout instead.</summary>
    public sealed class NeedsCheckout : Exception { public NeedsCheckout(string message) : base(message) { } }

    /// <summary>POST /api/hc/topup: 1-click, the saved card pays for the bundle. Returns (message, null) when paid, or
    /// (null, url) when the bank wants 3-D Secure on Stripe's page.</summary>
    public static async Task<(string? Paid, string? ConfirmUrl)> TopUp(int usd, string requestId, string token)
    {
        var (json, status) = await Send("/api/hc/topup", new JsonObject { ["usd"] = usd, ["request_id"] = requestId }, token);
        var msg = Message(json);
        var error = Str(json, "error");
        if (status is >= 200 and < 300)
        {
            if (Str(json, "status") == "requires_action" && WebUrl(json) is { } url) return (null, url);
            return (msg ?? "Payment made. The hours show in a few seconds.", null);
        }
        if (status == 402) throw new Failure(msg ?? "Your card was declined.");
        if (status == 409 && error is "no_card" or "needs_checkout") throw new NeedsCheckout(msg ?? "");
        if (status == 503) throw new Failure("Buying credit isn't set up yet. Write to support@hearchurch.com.");
        Check(json, status);
        return (msg ?? "", null);
    }

    /// <summary>POST /api/hc/auto-topup: automatic top-up on or off, and its bundle.</summary>
    public static async Task SetAutoTopUp(bool enabled, int usd, string token)
    {
        var (json, status) = await Send("/api/hc/auto-topup", new JsonObject { ["enabled"] = enabled, ["usd"] = usd }, token);
        Check(json, status);
    }

    /// <summary>POST /api/hc/web/link: a one-time link that opens hearchurch.com/account signed in, on Billing.
    /// An older hub without it: the plain account page.</summary>
    public static async Task<string> AccountLink(string token)
    {
        var plain = AppPaths.HubPath("account").ToString();
        try
        {
            var (json, status) = await Send("/api/hc/web/link", new JsonObject(), token);
            return status is >= 200 and < 300 && WebUrl(json) is { } url ? url : plain;
        }
        catch { return plain; }
    }

    /// <summary>POST /api/hc/slug: the listener link becomes hearchurch.com/&lt;slug&gt;; the old one keeps redirecting.</summary>
    public static async Task<string> ChangeSlug(string slug, string token)
    {
        var (json, status) = await Send("/api/hc/slug", new JsonObject { ["slug"] = slug }, token);
        Check(json, status);
        return Str(json, "slug", slug);
    }

    /// <summary>POST /api/hc/logs (kind "manual"): "Send logs to support". Lines from the engine's scrubbed diagnostic log,
    /// newest kept when there are too many; `facts` is one more line about the app. Returns the hub's reference.</summary>
    public static async Task<int> SendLogs(List<JsonObject> lines, string facts, string note, string token)
    {
        var kept = new List<JsonNode> { new JsonObject { ["t"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0, ["lvl"] = "INFO", ["src"] = "app", ["msg"] = facts } };
        var size = 0;
        for (var i = lines.Count - 1; i >= 0; i--)   // newest first, up to ~900 KB (the hub takes 1 MB) and 4,000 lines
        {
            var n = lines[i].ToJsonString().Length + 1;
            if (size + n > 900_000 || kept.Count >= 4000) break;
            size += n;
            kept.Add(lines[i].DeepClone());
        }
        var first = kept[0];
        kept.RemoveAt(0);
        kept.Reverse();
        kept.Insert(0, first);
        var body = new JsonObject { ["kind"] = "manual", ["lines"] = new JsonArray(kept.ToArray()), ["note"] = note.Length > 1000 ? note[..1000] : note };
        var (json, status) = await Send("/api/hc/logs", body, token);
        Check(json, status);
        return Num(json, "ref") is { } r ? (int)r : throw new Failure("The logs were not stored. Try again later.");
    }

    /// <summary>Throws for anything but a 2xx: NotAvailable (older hub), SignedOut (403), or the hub's message.</summary>
    private static void Check(JsonObject json, int status)
    {
        if (status is >= 200 and < 300) return;
        if (MissingRoute(json, status)) throw new NotAvailable();
        if (status == 403) throw new SignedOut();
        if (status == 429) throw new Failure(Message(json) ?? "Too many tries. Try again later.");
        throw new Failure(Message(json) ?? $"Server error {status}.");
    }

    /// <summary>An http(s) "url" from the hub, or null: nothing else is opened in the browser.</summary>
    private static string? WebUrl(JsonObject json) =>
        Uri.TryCreate(Str(json, "url"), UriKind.Absolute, out var u) && u.Scheme is "https" or "http" ? u.ToString() : null;

    private static double? Num(JsonObject o, string key) => Num(o[key]);

    private static double? Num(JsonNode? n)
    {
        try { return n is JsonValue v && v.TryGetValue<double>(out var d) ? d : null; } catch { return null; }
    }

    private static bool Bool(JsonObject o, string key)
    {
        try { return o[key] is JsonValue v && v.TryGetValue<bool>(out var b) && b; } catch { return false; }
    }

    private static DateTime? Date(double? unix) => unix is { } s ? DateTime.UnixEpoch.AddSeconds(s).ToLocalTime() : null;

    /// <summary>Is the hub reachable at all (preflight)?</summary>
    public static async Task<bool> Reachable()
    {
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(8));
            using var resp = await Http.GetAsync(AppPaths.HubPath("health"), cts.Token);
            return (int)resp.StatusCode == 200;
        }
        catch { return false; }
    }

    public static bool IsSubscriptionToken(string key) => key.StartsWith("sk-ant-oat", StringComparison.Ordinal);

    /// <summary>A free call that proves the key works: list the models it may use. Null if it works, or why not.</summary>
    public static async Task<string?> CheckApiKey(string key)
    {
        if (IsSubscriptionToken(key))
            return key.Length >= 40 && key.All(c => char.IsLetterOrDigit(c) || c is '-' or '_') ? null : "That token looks incomplete. Copy it again.";
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, "https://api.anthropic.com/v1/models?limit=1");
            req.Headers.Add("x-api-key", key);
            req.Headers.Add("anthropic-version", "2023-06-01");
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(15));
            using var resp = await Http.SendAsync(req, cts.Token);
            return (int)resp.StatusCode switch
            {
                200 => null,
                401 => "This key was not accepted. Copy it again from the console.",
                403 => "This key is not allowed to use the API (check the workspace).",
                var code => $"The key check answered {code}. Try again in a minute.",
            };
        }
        catch { return "Cannot check the key right now. Is the internet connected?"; }
    }

    /// <summary>"7kqm x2pa" → "7KQM-X2PA" while typing: letters and digits only, at most 8.</summary>
    public static string PairingFormat(string text)
    {
        var chars = new string(text.ToUpperInvariant().Where(c => c < 128 && char.IsLetterOrDigit(c)).Take(8).ToArray());
        return chars.Length > 4 ? chars[..4] + "-" + chars[4..] : chars;
    }

    public static bool IsEmail(string s)
    {
        var t = s.Trim();
        var at = t.IndexOf('@');
        return at > 0 && t[(at + 1)..].Contains('.') && !t.Contains(' ');
    }

    // ----------------------------------------------------------------

    private static async Task<(JsonObject Json, int Status)> Send(string path, JsonObject body, string? token = null)
    {
        using var req = new HttpRequestMessage(HttpMethod.Post, AppPaths.HubPath(path))
        {
            Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json"),
        };
        return await Do(req, token);
    }

    private static async Task<(JsonObject Json, int Status)> Get(string path, string token)
    {
        using var req = new HttpRequestMessage(HttpMethod.Get, AppPaths.HubPath(path));
        return await Do(req, token);
    }

    private static async Task<(JsonObject Json, int Status)> Do(HttpRequestMessage req, string? token)
    {
        if (token != null) req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        req.Headers.Add("X-HC-Billing-Types", BillingTypes);
        HttpResponseMessage resp;
        try { resp = await Http.SendAsync(req); }
        catch { throw new Failure($"Cannot reach {AppPaths.HubHost}. Is the internet connected?"); }
        using (resp)
        {
            JsonObject json;
            try { json = JsonNode.Parse(await resp.Content.ReadAsStringAsync()) as JsonObject ?? new JsonObject(); }
            catch { json = new JsonObject(); }
            return (json, (int)resp.StatusCode);
        }
    }

    private static bool MissingRoute(JsonObject json, int status) =>
        (status == 404 || status == 405) && json["error"] == null && (Str(json, "detail") is "" or "Not Found" or "Method Not Allowed");

    private static string Str(JsonObject o, string key, string fallback = "")
    {
        try { return o[key] is JsonValue v && v.TryGetValue<string>(out var s) ? s : fallback; } catch { return fallback; }
    }

    /// <summary>Hub errors are {"error": code, "message": text}, or FastAPI's {"detail": …}.</summary>
    private static string? Message(JsonObject json)
    {
        var m = Str(json, "message");
        if (m.Length > 0) return m;
        var d = Str(json, "detail");
        if (d.Length > 0) return d;
        if (json["detail"] is JsonArray list)
            return string.Join("\n", list.OfType<JsonObject>().Select(e =>
            {
                var msg = Str(e, "msg");
                var field = (e["loc"] as JsonArray)?.LastOrDefault()?.ToString().Replace("_", " ");
                return field != null ? $"{field}: {msg}" : msg;
            }));
        return null;
    }

    private static Failure TooManyTries(JsonObject json)
    {
        double secs = 60;
        try { if (json["retry_after"] is JsonValue v) secs = v.GetValue<double>(); } catch { }
        var mins = Math.Max(1, (int)Math.Ceiling(secs / 60));
        return new Failure($"Too many tries. Try again in {mins} minute{(mins == 1 ? "" : "s")}.");
    }
}
