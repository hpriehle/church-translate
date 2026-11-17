using System.Text.Json;
using System.Text.Json.Nodes;

namespace HearChurch.Core;

/// <summary>Where things live. The data folder is %LOCALAPPDATA%\HearChurch (docs/windows-engine.md): settings, logs,
/// glossaries and every downloaded model. HEARCHURCH_HOME overrides it (CI and testing).</summary>
public static class AppPaths
{
    public static string Home
    {
        get
        {
            var h = Environment.GetEnvironmentVariable("HEARCHURCH_HOME");
            return string.IsNullOrEmpty(h)
                ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "HearChurch")
                : h;
        }
    }

    public static string Logs => Path.Combine(Home, "logs");

    /// <summary>hearchurch.com, or another hub for testing: HEARCHURCH_APP_HUB=http://127.0.0.1:8000 (or "HubURL" in
    /// app-settings.json), as the Mac's `defaults write … HubURL`.</summary>
    public static Uri Hub
    {
        get
        {
            var s = Environment.GetEnvironmentVariable("HEARCHURCH_APP_HUB");
            if (string.IsNullOrEmpty(s)) s = Prefs.GetString("HubURL");
            if (string.IsNullOrEmpty(s)) s = "https://hearchurch.com";
            return Uri.TryCreate(s.TrimEnd('/'), UriKind.Absolute, out var u) ? u : new Uri("https://hearchurch.com");
        }
    }

    public static string HubHost => Hub.Host;

    public static Uri HubPath(string path) => new(Hub.GetLeftPart(UriPartial.Authority) + "/" + path.TrimStart('/'));
}

/// <summary>The app's own small settings (the Mac's UserDefaults): app-settings.json in the data folder.</summary>
public static class Prefs
{
    private static readonly object Gate = new();
    private static JsonObject? _cache;
    private static string File => Path.Combine(AppPaths.Home, "app-settings.json");

    private static JsonObject Load()
    {
        if (_cache != null) return _cache;
        try { _cache = JsonNode.Parse(System.IO.File.ReadAllText(File)) as JsonObject; } catch { }
        return _cache ??= new JsonObject();
    }

    private static void Save()
    {
        try
        {
            Directory.CreateDirectory(AppPaths.Home);
            var tmp = File + ".tmp";
            System.IO.File.WriteAllText(tmp, Load().ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
            System.IO.File.Move(tmp, File, true);
        }
        catch { }
    }

    public static string? GetString(string key)
    {
        lock (Gate) { return Load()[key]?.GetValue<string>(); }
    }

    public static bool GetBool(string key)
    {
        lock (Gate) { try { return Load()[key]?.GetValue<bool>() ?? false; } catch { return false; } }
    }

    public static void Set(string key, string? value)
    {
        lock (Gate) { if (value == null) Load().Remove(key); else Load()[key] = value; Save(); }
    }

    public static void Set(string key, bool value)
    {
        lock (Gate) { Load()[key] = value; Save(); }
    }

    public static void Remove(string key)
    {
        lock (Gate) { Load().Remove(key); Save(); }
    }
}
