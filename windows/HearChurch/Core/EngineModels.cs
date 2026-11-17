using System.Text.Json;
using System.Text.Json.Serialization;

namespace HearChurch.Core;

// What the engine sends (only the parts the app shows): the Mac's Engine.swift structs, snake_case on the wire.

public static class EngineJson
{
    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
        NumberHandling = JsonNumberHandling.AllowReadingFromString,
    };

    public static T? Parse<T>(string json) where T : class
    {
        try { return JsonSerializer.Deserialize<T>(json, Options); } catch { return null; }
    }
}

public sealed class Listeners
{
    public int Stream { get; set; }
    public int Captions { get; set; }
}

public sealed class EngineStatus
{
    public sealed class PipelineInfo
    {
        public bool Running { get; set; }
        public bool? Capturing { get; set; }
        public string? Source { get; set; }
        public double? LevelDb { get; set; }
        public bool? SttReady { get; set; }
        public double? StartedAt { get; set; }
        public double? BacklogS { get; set; }
        public Dictionary<string, double>? BehindS { get; set; }
        public string? LastError { get; set; }
        public string? MeetingId { get; set; }
        public string? SttEngine { get; set; }
        public string? SttModel { get; set; }
        public double? SttRtf { get; set; }
        public double? SttWaitS { get; set; }
        public int? SttSkipped { get; set; }
        public string? SttSteppedDown { get; set; }
        public Dictionary<string, Channel>? Channels { get; set; }
        public MeetingStats? MeetingStats { get; set; }
    }

    public sealed class Channel
    {
        public sealed class TtsInfo { public string? Engine { get; set; } public string? Voice { get; set; } public string? Error { get; set; } }
        public double? BacklogS { get; set; }
        public double? Speed { get; set; }
        public bool? Condense { get; set; }
        public Dictionary<string, int>? Queues { get; set; }
        public TtsInfo? Tts { get; set; }
        public Listeners? Listeners { get; set; }
    }

    public sealed class MeetingStats
    {
        public sealed class Lang
        {
            public int Lines { get; set; }
            public int Captioned { get; set; }
            public int Aired { get; set; }
            public int Dropped { get; set; }
            public int Skipped { get; set; }
            public int Failed { get; set; }
            public double? CaptionP50 { get; set; }
            public double? CaptionP95 { get; set; }
            public double? AudioP50 { get; set; }
            public double? AudioP95 { get; set; }
        }
        public int Lines { get; set; }
        public int Skipped { get; set; }
        public int Failed { get; set; }
        public double SpeechS { get; set; }
        public Dictionary<string, Lang> Langs { get; set; } = new();
    }

    public sealed class Translate
    {
        public sealed class ClaudeInfo { public string State { get; set; } = ""; public string? Model { get; set; } public double? BusyFor { get; set; } }
        public ClaudeInfo Claude { get; set; } = new();
        public string? LastError { get; set; }
    }

    public sealed class Link
    {
        public bool? Connected { get; set; }
        public bool? Live { get; set; }
        public string? Error { get; set; }
        public string? Notice { get; set; }
        public Listeners? Listeners { get; set; }
        public Dictionary<string, Listeners>? ListenersByLang { get; set; }
    }

    public sealed class EngineInfo { public Link? Client { get; set; } }

    public sealed class Meeting
    {
        public List<string> Speakers { get; set; } = new();
        public List<string> Targets { get; set; } = new();
    }

    public PipelineInfo Pipeline { get; set; } = new();
    [JsonPropertyName("translate")] public Dictionary<string, Translate> TranslateBy { get; set; } = new();
    public EngineInfo Engine { get; set; } = new();
    public Listeners Listeners { get; set; } = new();
    public string? ListenerUrl { get; set; }
    public string? Sharing { get; set; }
    public string? ClaudeAccess { get; set; }
    [JsonPropertyName("meeting")] public Meeting? MeetingInfo { get; set; }
    public string? Source { get; set; }
    public List<string>? AutoSpeakers { get; set; }
    public string? MicWarning { get; set; }
    public string? MicName { get; set; }
}

public sealed class PackageStatus
{
    public string Id { get; set; } = "";
    public string Name { get; set; } = "";
    public string State { get; set; } = "";
    public string Error { get; set; } = "";
    public double SizeMb { get; set; }
    public double DoneMb { get; set; }

    public string Key => $"{Id}|{State}|{Error}|{SizeMb}|{DoneMb}";
}

public sealed class PackageList { public List<PackageStatus> Packages { get; set; } = new(); }

/// <summary>GET /api/state: the settings the operator can change, and what there is to choose from.</summary>
public sealed class EngineSettings
{
    public sealed class SettingsInfo
    {
        public sealed class AudioInfo
        {
            public string InputDevice { get; set; } = "";
            public int InputChannel { get; set; }
            public double InputGain { get; set; } = 1;
        }
        public sealed class ServerInfo { public string? Sharing { get; set; } }
        public sealed class TtsInfo
        {
            public string Engine { get; set; } = "";
            public string Voice { get; set; } = "";
            public double Volume { get; set; } = 1;
            public double? Speed { get; set; }
        }
        public sealed class TranslateInfo { public string ClaudeModel { get; set; } = ""; }
        public sealed class VoiceInfo
        {
            public string Engine { get; set; } = "";
            public string Voice { get; set; } = "";
            public double? Speed { get; set; }
        }
        public AudioInfo Audio { get; set; } = new();
        public ServerInfo Server { get; set; } = new();
        public EngineStatus.Meeting? Meeting { get; set; }
        public TtsInfo Tts { get; set; } = new();
        public TranslateInfo Translate { get; set; } = new();
        public Dictionary<string, VoiceInfo> Voices { get; set; } = new();
    }

    /// <summary>A voice: "piper" + "fr_FR-siwis-medium", and the name the operator sees.</summary>
    public sealed class VoiceOption
    {
        public string Id { get; set; } = "";
        public string Engine { get; set; } = "";
        public string Voice { get; set; } = "";
        public string Label { get; set; } = "";
        public string Key => $"{Engine}:{Voice}";
    }

    public sealed class Device
    {
        public string Name { get; set; } = "";
        public int Channels { get; set; }
        public bool? Default { get; set; }
    }

    public SettingsInfo Settings { get; set; } = new();
    public List<Device> Devices { get; set; } = new();
    /// <summary>Downloaded voices, per language: these can be chosen.</summary>
    public Dictionary<string, List<VoiceOption>> Voices { get; set; } = new();
    /// <summary>Every voice, downloaded or not, so they can be heard.</summary>
    public Dictionary<string, List<VoiceOption>> VoiceCatalog { get; set; } = new();

    public string Key => $"{Settings.Audio.InputDevice}|{Settings.Audio.InputChannel}|{Settings.Audio.InputGain}|{Settings.Server.Sharing}|"
        + string.Join(",", Devices.Select(d => $"{d.Name}:{d.Channels}:{d.Default}")) + "|" + VoiceKey
        + $"|{Settings.Tts.Volume}|{Settings.Tts.Speed}|{Settings.Translate.ClaudeModel}|"
        + string.Join(",", Settings.Voices.OrderBy(v => v.Key).Select(v => $"{v.Key}:{v.Value.Speed}"));

    /// <summary>Changes when the voices to choose from, or the chosen ones, change.</summary>
    public string VoiceKey => $"{Settings.Tts.Engine}:{Settings.Tts.Voice}|"
        + string.Join(",", Settings.Voices.OrderBy(v => v.Key).Select(v => $"{v.Key}={v.Value.Engine}:{v.Value.Voice}")) + "|"
        + string.Join(",", Voices.OrderBy(v => v.Key).Select(v => $"{v.Key}:{v.Value.Count}"))
        + string.Join(",", VoiceCatalog.OrderBy(v => v.Key).Select(v => $"{v.Key}:{v.Value.Count}"));
}

/// <summary>One stretch of speech: what was heard and what each language made of it.</summary>
public sealed class Chunk
{
    public sealed class Out
    {
        public string Text { get; set; } = "";
        public string Status { get; set; } = "";
        public string? Note { get; set; }
        public double? CaptionLag { get; set; }
        public string? Engine { get; set; }
        public double? AirLag { get; set; }
        public double? MtMs { get; set; }
        public double? TtsMs { get; set; }
        public double? Speed { get; set; }
        public bool? Condensed { get; set; }
        [JsonIgnore] public bool Original => Engine == "original";
    }

    public int Seq { get; set; }
    public string? Meeting { get; set; }
    public double? AudioDurS { get; set; }
    public double? SttMs { get; set; }
    public string? CutReason { get; set; }
    public string En { get; set; } = "";
    public string? Src { get; set; }
    public string Status { get; set; } = "";
    public string? Note { get; set; }
    public double? WallTime { get; set; }
    public Dictionary<string, Out> Langs { get; set; } = new();

    /// <summary>Changes whenever anything shown in its row changes.</summary>
    [JsonIgnore]
    public string Key => $"{Seq}|{En}|{Src}|{Status}|" + string.Join(";", Langs.OrderBy(k => k.Key)
        .Select(k => $"{k.Key}:{k.Value.Status}:{k.Value.Text}:{k.Value.CaptionLag}:{k.Value.AirLag}"));
}

public sealed record LogLine(string Level, string Text);

/// <summary>The engine's scrubbed diagnostic log (app/sharelog.py: logs\shared.log and its rotations, JSON lines),
/// for "Send logs to support". Technical only: never anything that was said.</summary>
public static class DiagnosticLog
{
    public static List<System.Text.Json.Nodes.JsonObject> LastDays(double days)
    {
        var since = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - days * 86400;
        var o = new List<System.Text.Json.Nodes.JsonObject>();
        foreach (var name in new[] { "shared.log.3", "shared.log.2", "shared.log.1", "shared.log" })
        {
            string[] lines;
            try { lines = File.ReadAllLines(Path.Combine(AppPaths.Logs, name)); } catch { continue; }
            foreach (var line in lines)
            {
                try
                {
                    if (System.Text.Json.Nodes.JsonNode.Parse(line) is System.Text.Json.Nodes.JsonObject obj
                        && obj["t"] is System.Text.Json.Nodes.JsonValue t && t.TryGetValue<double>(out var at) && at > since
                        && obj["msg"] is System.Text.Json.Nodes.JsonValue m && m.TryGetValue<string>(out _))
                        o.Add(obj);
                }
                catch { }
            }
        }
        return o;
    }
}
