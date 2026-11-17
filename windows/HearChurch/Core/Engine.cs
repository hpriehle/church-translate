using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace HearChurch.Core;

/// <summary>Runs the engine sidecar (engine\hearchurch-engine.exe --app), restarts it if it dies, and follows it: status
/// and downloads by polling, what was said (and its translations) over /ws/operator. The Mac's Engine.swift; how to
/// start and talk to it is in docs/windows-engine.md. Everything here runs on the UI thread (`post`).
/// For development, HEARCHURCH_APP_ENGINE=path\to\hearchurch-engine.exe runs another build.</summary>
public sealed class Engine
{
    public enum RunState { Stopped, Starting, Running, Failed }

    public RunState State { get; private set; } = RunState.Stopped;
    public string FailReason { get; private set; } = "";
    public EngineStatus? Status { get; private set; }
    public List<PackageStatus> Packages { get; private set; } = new();
    public EngineSettings? Settings { get; private set; }
    public List<Chunk> Chunks { get; } = new();              // newest first, the current (or last) meeting's only
    public string? MeetingId { get; private set; }
    public List<string> MeetingTargets { get; private set; } = new();
    public List<LogLine> Log { get; } = new();               // newest first
    public int Port { get; private set; }
    public string BaseUrl => $"http://127.0.0.1:{Port}";

    /// <summary>Anything shown changed (status, packages, settings, transcript, log).</summary>
    public event Action? Changed;
    public Action? OnRunning;

    private List<string> _packageLanguages = new();
    public List<string> PackageLanguages
    {
        get => _packageLanguages;
        set { if (!value.SequenceEqual(_packageLanguages)) { _packageLanguages = value; _ = RefreshPackages(); } }
    }

    private readonly Action<Action> _post;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(30) };
    private Process? _process;
    private bool _wantRunning;
    private int _restarts;
    private CancellationTokenSource? _poll, _feed;
    private Dictionary<string, string> _environment = new();
    private int _launchCount;
    private DateTime _launchedAt = DateTime.UtcNow;
    private const double StartDeadline = 90;
    private StreamWriter? _logWriter;
    private readonly object _logGate = new();
    private string _statusJson = "";

    public static string LogPath => Path.Combine(AppPaths.Logs, "engine-output.log");
    private static string PidPath => Path.Combine(AppPaths.Home, "engine.pid");

    public Engine(Action<Action> post) { _post = post; }

    private void Emit() => Changed?.Invoke();

    // ---------------------------------------------------------------- lifecycle

    public void Start(Dictionary<string, string> env)
    {
        _environment = env;
        _wantRunning = true;
        _restarts = 0;
        Launch();
    }

    /// <summary>New key or device token: the engine reads them at start, so start it again.</summary>
    public void Restart(Dictionary<string, string> env)
    {
        _environment = env;
        _wantRunning = true;
        _restarts = 0;
        if (_process is { HasExited: false } p) Kill(p);   // the exit handler launches it again
        else Launch();
    }

    public void Stop()
    {
        _wantRunning = false;
        TryDelete(PidPath);
        _poll?.Cancel();
        _feed?.Cancel();
        if (_process is { HasExited: false } p)
        {
            Kill(p);
            try { p.WaitForExit(5000); } catch { }
        }
    }

    /// <summary>The exe: next to the app (engine\hearchurch-engine.exe), or HEARCHURCH_APP_ENGINE.</summary>
    public static string? EnginePath()
    {
        var dev = Environment.GetEnvironmentVariable("HEARCHURCH_APP_ENGINE");
        var exe = !string.IsNullOrEmpty(dev) ? dev : Path.Combine(AppContext.BaseDirectory, "engine", "hearchurch-engine.exe");
        return File.Exists(exe) ? Path.GetFullPath(exe) : null;
    }

    private void Launch()
    {
        var exe = EnginePath();
        if (exe == null)
        {
            Fail("The translation engine is missing from this copy of the app. Download it again.");
            return;
        }
        ReapOrphan();
        Port = FreePort();
        var psi = new ProcessStartInfo(exe)
        {
            WorkingDirectory = Path.GetDirectoryName(exe)!,
            UseShellExecute = false,
            CreateNoWindow = true,                   // CREATE_NO_WINDOW
            RedirectStandardInput = true,            // a console-less engine needs explicit handles (WinError 50)
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var a in new[] { "--app", "--no-browser", "--port", Port.ToString() }) psi.ArgumentList.Add(a);
        foreach (var key in psi.Environment.Keys.ToList())
            if (key.StartsWith("HEARCHURCH_", StringComparison.OrdinalIgnoreCase) || key is "ANTHROPIC_API_KEY" or "CLAUDE_CODE_OAUTH_TOKEN")
                psi.Environment.Remove(key);
        psi.Environment["HEARCHURCH_HOME"] = AppPaths.Home;
        psi.Environment["PYTHONUNBUFFERED"] = "1";
        foreach (var (k, v) in _environment) psi.Environment[k] = v;

        Directory.CreateDirectory(AppPaths.Logs);
        _launchCount++;
        TrimLog(LogPath);
        OpenLog($"\n===== {DateTime.UtcNow:yyyy-MM-ddTHH:mm:ssZ} launch #{_launchCount} (attempt {_restarts + 1}) =====\n");

        var p = new Process { StartInfo = psi, EnableRaisingEvents = true };
        p.OutputDataReceived += (_, e) => WriteLog(e.Data);
        p.ErrorDataReceived += (_, e) => WriteLog(e.Data);
        p.Exited += (_, _) => _post(() => Exited(p));
        try
        {
            p.Start();
            p.BeginOutputReadLine();
            p.BeginErrorReadLine();
        }
        catch (Exception e)
        {
            Fail($"Could not start the translation engine: {e.Message}");
            return;
        }
        _process = p;
        try { File.WriteAllText(PidPath, p.Id.ToString()); } catch { }
        _launchedAt = DateTime.UtcNow;
        State = RunState.Starting;
        Emit();
        Poll();
    }

    private void Fail(string why)
    {
        State = RunState.Failed;
        FailReason = why;
        Emit();
    }

    private void Exited(Process p)
    {
        if (!ReferenceEquals(p, _process)) return;
        _process = null;
        Status = null;
        _statusJson = "";
        _feed?.Cancel();
        TryDelete(PidPath);
        WriteLog($"===== exited ({SafeExitCode(p)}) =====");
        if (!_wantRunning)
        {
            if (State != RunState.Failed) State = RunState.Stopped;   // keep the reason when it was stopped for failing
            Emit();
            return;
        }
        _restarts++;
        if (_restarts > 5)
        {
            Fail($"The translation engine keeps stopping. See logs in {AppPaths.Logs}.");
            return;
        }
        State = RunState.Starting;
        Emit();
        var delay = Math.Min(30, 2 << _restarts);
        _ = Task.Delay(TimeSpan.FromSeconds(delay)).ContinueWith(_ => _post(() =>
        {
            if (_wantRunning && _process == null) Launch();
        }));
    }

    private static string SafeExitCode(Process p)
    {
        try { return $"exit {p.ExitCode}"; } catch { return "exit ?"; }
    }

    // ---------------------------------------------------------------- status, downloads, settings

    private void Poll()
    {
        _poll?.Cancel();
        var cts = _poll = new CancellationTokenSource();
        _ = PollLoop(cts.Token);
    }

    private async Task PollLoop(CancellationToken ct)
    {
        var tick = 0;
        while (!ct.IsCancellationRequested)
        {
            var (json, s) = await GetWithText<EngineStatus>("api/status");
            if (ct.IsCancellationRequested) return;
            if (s != null)
            {
                if (State != RunState.Running)
                {
                    State = RunState.Running;
                    _restarts = 0;
                    tick = 0;
                    Follow();
                    await LoadSettings();
                    OnRunning?.Invoke();
                }
                if (json != _statusJson)
                {
                    _statusJson = json;
                    Status = s;
                    FollowMeeting(s);
                    Emit();
                }
                if (tick % 10 == 0 || Packages.Any(p => p.State == "downloading")) await RefreshPackages();
                tick++;
            }
            else if (State == RunState.Running)
            {
                if (Status != null) { Status = null; _statusJson = ""; Emit(); }   // briefly unreachable
            }
            else if (State == RunState.Starting && _process != null && (DateTime.UtcNow - _launchedAt).TotalSeconds > StartDeadline)
            {
                GiveUpStarting();
                return;
            }
            // Once a second during a meeting; every 2 s while idle; fast while starting.
            var wait = State != RunState.Running ? 0.5 : (Status?.Pipeline.Running == true ? 1 : 2);
            try { await Task.Delay(TimeSpan.FromSeconds(wait), ct); } catch { return; }
        }
    }

    private void GiveUpStarting()
    {
        _wantRunning = false;
        Fail($"The translation engine did not start within {(int)StartDeadline} seconds. Restart it, and if that does not help, show the log.");
        if (_process is { HasExited: false } p) Kill(p);
    }

    public async Task RefreshPackages()
    {
        if (State != RunState.Running) return;
        var q = string.Join(",", PackageLanguages);
        var p = await Get<PackageList>($"api/packages?languages={q}");
        if (p == null) return;
        if (string.Join(";", p.Packages.Select(x => x.Key)) != string.Join(";", Packages.Select(x => x.Key)))
        {
            Packages = p.Packages;
            Emit();
        }
    }

    /// <summary>Start downloading whatever these languages still need (speech recognition included).</summary>
    public async Task Download(IEnumerable<string> languages)
    {
        var (_, ok) = await Send("api/packages/download", new JsonObject { ["languages"] = new JsonArray(languages.Select(l => (JsonNode)l!).ToArray()) });
        if (ok) await RefreshPackages();
    }

    public async Task LoadSettings()
    {
        var s = await Get<EngineSettings>("api/state");
        if (s != null && s.Key != Settings?.Key)
        {
            Settings = s;
            Emit();
        }
    }

    /// <summary>Change engine settings (POST /api/settings), e.g. {"audio": {"input_gain": 1.5}}. Returns why not, or null.</summary>
    public async Task<string?> Set(JsonObject body)
    {
        var (data, ok) = await Send("api/settings", body);
        await LoadSettings();
        return ok ? null : Detail(data);
    }

    public async Task<(bool Ok, string Message)> Post(string path, JsonObject? body = null)
    {
        var (data, ok) = await Send(path, body ?? new JsonObject());
        return (ok, Detail(data) ?? "");
    }

    public async Task<T?> Get<T>(string path) where T : class => (await GetWithText<T>(path)).Value;

    private async Task<(string Text, T? Value)> GetWithText<T>(string path) where T : class
    {
        if (Port == 0) return ("", null);
        try
        {
            using var resp = await _http.GetAsync($"{BaseUrl}/{path}");
            if (resp.StatusCode != HttpStatusCode.OK) return ("", null);
            var text = await resp.Content.ReadAsStringAsync();
            return (text, EngineJson.Parse<T>(text));
        }
        catch { return ("", null); }
    }

    private async Task<(string Data, bool Ok)> Send(string path, JsonNode body)
    {
        if (Port == 0) return ("", false);
        try
        {
            using var content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
            using var resp = await _http.PostAsync($"{BaseUrl}/{path}", content);
            return (await resp.Content.ReadAsStringAsync(), resp.StatusCode == HttpStatusCode.OK);
        }
        catch
        {
            return ("{\"detail\": \"The translation engine is not answering.\"}", false);
        }
    }

    private static string? Detail(string data)
    {
        try
        {
            var o = JsonNode.Parse(data) as JsonObject;
            if (o?["detail"] is JsonValue d && d.TryGetValue<string>(out var s)) return s;
            if (o?["message"] is JsonValue m && m.TryGetValue<string>(out var t)) return t;
        }
        catch { }
        return null;
    }

    // ---------------------------------------------------------------- live feed (/ws/operator)

    private void Follow()
    {
        _feed?.Cancel();
        var cts = _feed = new CancellationTokenSource();
        var url = new Uri($"ws://127.0.0.1:{Port}/ws/operator");
        _ = Task.Run(async () =>
        {
            var buf = new byte[64 * 1024];
            while (!cts.IsCancellationRequested)
            {
                using var ws = new ClientWebSocket();
                try
                {
                    await ws.ConnectAsync(url, cts.Token);
                    var msg = new MemoryStream();
                    while (!cts.IsCancellationRequested && ws.State == WebSocketState.Open)
                    {
                        var r = await ws.ReceiveAsync(buf, cts.Token);
                        if (r.MessageType == WebSocketMessageType.Close) break;
                        msg.Write(buf, 0, r.Count);
                        if (!r.EndOfMessage) continue;
                        var text = Encoding.UTF8.GetString(msg.ToArray());
                        msg.SetLength(0);
                        if (r.MessageType == WebSocketMessageType.Text) _post(() => Handle(text));
                    }
                }
                catch { }
                try { await Task.Delay(1000, cts.Token); } catch { return; }
            }
        });
    }

    /// <summary>A new meeting: the transcript starts empty, with the new meeting's columns.</summary>
    private void NewMeeting(string id, List<string>? targets)
    {
        if (id != MeetingId)
        {
            MeetingId = id;
            Chunks.Clear();
        }
        if (targets != null) MeetingTargets = targets;
    }

    private void FollowMeeting(EngineStatus s)
    {
        if (s.Pipeline.MeetingId is not { } id) return;
        NewMeeting(id, s.Pipeline.Running ? s.MeetingInfo?.Targets : null);
    }

    public void Handle(string text)
    {
        JsonObject? json;
        try { json = JsonNode.Parse(text) as JsonObject; } catch { return; }
        if (json == null) return;
        switch ((string?)json["type"])
        {
            case "meeting":
                if (json["meeting"] is not JsonValue mv || !mv.TryGetValue<string>(out var id)) return;
                NewMeeting(id, json["targets"] is JsonArray a ? a.Select(x => (string?)x ?? "").ToList() : null);
                Emit();
                break;
            case "chunk":
                var c = EngineJson.Parse<Chunk>(text);
                if (c == null) return;
                if (!string.IsNullOrEmpty(c.Meeting))
                {
                    if (MeetingId == null) MeetingId = c.Meeting;
                    else if (c.Meeting != MeetingId) return;   // a line still finishing from an earlier meeting
                }
                else if (Chunks.Count > 0 && c.Seq < Chunks[0].Seq)
                {
                    Chunks.Clear();
                }
                var i = Chunks.FindIndex(x => x.Seq == c.Seq);
                if (i >= 0) Chunks[i] = c;
                else
                {
                    Chunks.Insert(0, c);
                    if (Chunks.Count > 300) Chunks.RemoveRange(300, Chunks.Count - 300);
                }
                Emit();
                break;
            case "log":
                Log.Insert(0, new LogLine((string?)json["level"] ?? "info", (string?)json["text"] ?? ""));
                if (Log.Count > 200) Log.RemoveAt(Log.Count - 1);
                Emit();
                break;
        }
    }

    // ---------------------------------------------------------------- helpers

    private void OpenLog(string header)
    {
        lock (_logGate)
        {
            try
            {
                _logWriter?.Dispose();
                _logWriter = new StreamWriter(new FileStream(LogPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite), new UTF8Encoding(false)) { AutoFlush = true };
                _logWriter.Write(header);
            }
            catch { _logWriter = null; }
        }
    }

    private void WriteLog(string? line)
    {
        if (line == null) return;
        lock (_logGate) { try { _logWriter?.WriteLine(line); } catch { } }
    }

    /// <summary>Keep engine-output.log to about 2 MB (the end of it).</summary>
    private static void TrimLog(string path)
    {
        const int limit = 2_000_000;
        try
        {
            var info = new FileInfo(path);
            if (!info.Exists || info.Length <= limit + 500_000) return;
            byte[] tail;
            using (var f = File.OpenRead(path))
            {
                f.Seek(-limit, SeekOrigin.End);
                tail = new byte[limit];
                f.ReadExactly(tail);
            }
            File.WriteAllBytes(path, tail);
        }
        catch { }
    }

    /// <summary>If the pid saved by the last launch is still alive and is our engine, stop it.</summary>
    private static void ReapOrphan()
    {
        try
        {
            if (!File.Exists(PidPath)) return;
            if (int.TryParse(File.ReadAllText(PidPath).Trim(), out var pid) && pid > 0 && pid != Environment.ProcessId)
            {
                using var p = Process.GetProcessById(pid);
                if (p.ProcessName.Equals("hearchurch-engine", StringComparison.OrdinalIgnoreCase))
                {
                    p.Kill(true);
                    p.WaitForExit(3000);
                }
            }
        }
        catch { }
        finally { TryDelete(PidPath); }
    }

    private static void Kill(Process p)
    {
        try { p.Kill(true); } catch { }
    }

    private static void TryDelete(string path)
    {
        try { File.Delete(path); } catch { }
    }

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }
}
