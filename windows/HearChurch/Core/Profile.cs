using System.Globalization;
using System.Text.Json;

namespace HearChurch.Core;

/// <summary>What setup chose, plus what hearchurch.com handed back (profile.json, the Mac's fields and names).
/// Secrets (API key, device token) are not in here: they live in Windows Credential Manager.</summary>
public sealed class Profile
{
    public string Church { get; set; } = "";
    public string UnitType { get; set; } = "ward";
    public string Name { get; set; } = "";
    public string Stake { get; set; } = "";
    public string Address { get; set; } = "";
    public List<string> Speakers { get; set; } = new() { "en" };
    public List<string> Targets { get; set; } = new() { "fr" };
    public bool MeetingPicked { get; set; }
    public bool Approved { get; set; }
    public string Slug { get; set; } = "";
    public string Claim { get; set; } = "";
    public bool LanguagesPicked { get; set; }
    public bool SetupDone { get; set; }
    public bool ShareLogs { get; set; } = true;

    private static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = true,
    };

    public static string FilePath => Path.Combine(AppPaths.Home, "profile.json");

    public static Profile Load()
    {
        try
        {
            var p = JsonSerializer.Deserialize<Profile>(File.ReadAllText(FilePath), Json) ?? new Profile();
            p.Speakers ??= new() { "en" };
            p.Targets ??= new() { "fr" };
            return p;
        }
        catch { return new Profile(); }
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(AppPaths.Home);
            var tmp = FilePath + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(this, Json));
            File.Move(tmp, FilePath, true);
        }
        catch { }
    }

    public Profile Copy() => (Profile)MemberwiseClone();
}

public enum LanguageRole { Speaker, Target }

/// <summary>The 12 languages: each can be a translation language (a listen pill), and each can be spoken.
/// As on the Intel Mac: CPU speech recognition, and no Wolof voice (it needs torch; docs/windows-engine.md).</summary>
public sealed record OutputLanguage(string Id, string Name, string Native, bool Available, double SizeMB)
{
    public const double SpeechDownloadMB = 634;          // faster-whisper small + base (the engine reports the real size)
    public const double WolofSpeechDownloadMB = 330;
    public const string WolofSpeechPackage = "speech-wo"; // the "wo" package is the Wolof voice

    public string? UnavailableReason => Available ? null
        : "No Wolof voice on Windows yet. Wolof can still be spoken: pick it as a speaker language.";

    public bool AvailableAs(LanguageRole role) => role == LanguageRole.Speaker || Available;
    public string? UnavailableReasonAs(LanguageRole role) => AvailableAs(role) ? null : UnavailableReason;

    public static string PackageId(string code, LanguageRole role) =>
        code == "wo" && role == LanguageRole.Speaker ? WolofSpeechPackage : code;

    public double SizeMBAs(LanguageRole role) => PackageId(Id, role) == WolofSpeechPackage ? WolofSpeechDownloadMB : SizeMB;

    public static readonly OutputLanguage English = new("en", "English", "English", true, 127);

    public static readonly IReadOnlyList<OutputLanguage> All = new List<OutputLanguage>
    {
        new("fr", "French", "Français", true, 127),
        new("wo", "Wolof", "Wolof", false, 634),
        new("es", "Spanish", "Español", true, 127),
        new("ru", "Russian", "Русский", true, 127),
        new("pt", "Portuguese", "Português", true, 126),
        new("uk", "Ukrainian", "Українська", true, 228),
        new("fa", "Persian", "فارسی", true, 127),
        new("ne", "Nepali", "नेपाली", true, 63),
        new("vi", "Vietnamese", "Tiếng Việt", true, 63),
        new("zh", "Chinese", "中文", true, 377),
        English,
        new("lv", "Latvian", "Latviešu", true, 64),
    };

    public static IReadOnlyList<OutputLanguage> Speakers => All;

    public static string NameOf(string code) => All.FirstOrDefault(l => l.Id == code)?.Name ?? code.ToUpperInvariant();
    public static string NativeOf(string code) => All.FirstOrDefault(l => l.Id == code)?.Native ?? code.ToUpperInvariant();

    /// <summary>"Russian, French and Latvian".</summary>
    public static string List(IReadOnlyList<string> names) =>
        names.Count <= 1 ? string.Concat(names) : string.Join(", ", names.Take(names.Count - 1)) + " and " + names[^1];
}

public static class Format
{
    private static readonly CultureInfo C = CultureInfo.InvariantCulture;

    public static string Size(double mb) =>
        mb >= 1000 ? (mb / 1000).ToString("0.0", C) + " GB" : Math.Round(mb).ToString("0", C) + " MB";

    /// <summary>754 s → "12 min"; under a minute → "40 s".</summary>
    public static string Minutes(double s) => s < 60 ? $"{(int)s} s" : $"{(int)Math.Round(s / 60)} min";

    /// <summary>A delay: "2.4 s", or "–" when there is none yet.</summary>
    public static string Seconds(double? s) => s is { } v ? v.ToString("0.0", C) + " s" : "–";

    public static string F(double v, string fmt) => v.ToString(fmt, C);

    public static string Elapsed(TimeSpan t)
    {
        var s = Math.Max(0, (int)t.TotalSeconds);
        return s >= 3600 ? $"{s / 3600}:{s / 60 % 60:00}:{s % 60:00}" : $"{s / 60}:{s % 60:00}";
    }
}

public static class DiagnosticLogText
{
    public const string Detail = "Technical details only: errors, timings, microphone level, languages, delays and versions. "
        + "Never anything that was said, recognised, translated or spoken. Kept 30 days.";
}
