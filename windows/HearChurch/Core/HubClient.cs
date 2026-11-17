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
        var (json, status) = await Send("/api/hc/pair", new JsonObject { ["code"] = code, ["device_name"] = deviceName });
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
