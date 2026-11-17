using System.Text.Json.Nodes;

namespace HearChurch.Core;

public sealed class Check
{
    public enum Kind { Ok, Warn, Bad, Pending }
    public required string Id { get; init; }
    public required string Title { get; init; }
    public required Kind State { get; init; }
    public string Detail { get; init; } = "";
    public (string Title, Action Run)? Action { get; init; }
}

/// <summary>Everything the windows show: the congregation, the engine, downloads and the checks before Start.
/// The Mac's AppModel.swift, minus what Windows doesn't have yet (Settings tabs, billing, updates: W3/W5).</summary>
public sealed class AppModel
{
    public Profile Profile { get; private set; } = Profile.Load();
    public enum KeyStates { Unknown, Checking, Ok, Missing, Bad }
    public KeyStates KeyState { get; private set; } = KeyStates.Unknown;
    public string KeyProblem { get; private set; } = "";
    public bool? HubReachable { get; private set; }
    public bool OnBattery { get; private set; } = WinSystem.OnBattery;
    public bool Online { get; private set; } = WinSystem.Online;
    public string? Message { get; set; }
    public DateTime? MeetingStarted { get; private set; }
    public string BillingMode { get; private set; } = Prefs.GetString("HubBilling") ?? "";

    public Engine Engine { get; }
    public MicMonitor Mic { get; }

    /// <summary>Anything shown changed. Raised on the UI thread.</summary>
    public event Action? Changed;
    /// <summary>A warning to show outside the window (the notification area), once until it clears.</summary>
    public Action<string, string>? Notify;
    public Action? OpenSettings;
    public Action? SetupFinished;
    public Action? SignedOutAction;

    private readonly Action<Action> _post;
    private DateTime _lastHubCheck = DateTime.MinValue;
    private bool _micWarningShown;
    private EngineStatus.Meeting? _lastEngineMeeting;

    public AppModel(Action<Action> post)
    {
        _post = post;
        Engine = new Engine(post);
        Mic = new MicMonitor(post);
        Engine.Changed += EngineChanged;
        Engine.OnRunning = EngineStarted;
        Mic.OnDevicesChanged = () => _ = Engine.LoadSettings();
        Mic.HeardChanged += Emit;
    }

    public void Emit() => Changed?.Invoke();

    private bool _wasRunning;

    private void EngineChanged()
    {
        var st = Engine.Status;
        if (st != null)
        {
            FollowRunning(st);
            MicWarningChanged(st);
            FollowEngineMeeting(st);
            if (st.Pipeline.Running != _wasRunning)
            {
                _wasRunning = st.Pipeline.Running;
                if (_wasRunning) Mic.Stop(); else StartMicPreview();
            }
        }
        else if (Engine.State != Engine.RunState.Running) WinSystem.KeepAwake(false);
        Emit();
    }

    // ---------------------------------------------------------------- what the windows show

    public bool Running => Engine.Status?.Pipeline.Running ?? false;
    public static string DeviceName => Environment.MachineName;

    public string? MicWarning => Running && Engine.Status?.MicWarning is { Length: > 0 } w ? w : null;
    public string? MicName => Running && Engine.Status?.MicName is { Length: > 0 } n ? n : null;

    /// <summary>Cloud: anyone with the link listens through hearchurch.com. Local: phones on this Wi-Fi only.</summary>
    public bool LocalSharing => (Engine.Settings?.Settings.Server.Sharing ?? Engine.Status?.Sharing) == "local";

    public string? ListenerUrl
    {
        get
        {
            if (LocalSharing) return string.IsNullOrEmpty(Engine.Status?.ListenerUrl) ? null : Engine.Status!.ListenerUrl;
            return Profile.Slug.Length == 0 ? null : AppPaths.HubPath(Profile.Slug).ToString();
        }
    }

    public int Listeners => LocalSharing ? Engine.Status?.Listeners.Captions ?? 0 : Engine.Status?.Engine.Client?.Listeners?.Captions ?? 0;

    /// <summary>Phones on one language now, if the engine / hub reports it per language.</summary>
    public int? ListenersOn(string code)
    {
        if (LocalSharing) return Engine.Status?.Pipeline.Channels?.GetValueOrDefault(code)?.Listeners?.Captions;
        var by = Engine.Status?.Engine.Client?.ListenersByLang;
        if (by != null && by.TryGetValue(code, out var l)) return l.Captions;
        return Listeners == 0 ? 0 : null;
    }

    public async Task SetSharing(bool local)
    {
        await Engine.Set(new JsonObject { ["server"] = new JsonObject { ["sharing"] = local ? "local" : "cloud" } });
        if (!Running) RelaunchEngine();   // the engine picks its network address when it starts
    }

    // ---------------------------------------------------------------- engine

    /// <summary>The engine runs from the first launch, so voices can download while the operator is still in setup.</summary>
    public void Launch()
    {
        Engine.PackageLanguages = OutputLanguage.All.Select(l => l.Id).Append(OutputLanguage.WolofSpeechPackage).ToList();
        Engine.Start(EngineEnvironment());
        _ = CheckKey();
        _ = RefreshBilling();
        _ = CheckHub();
        _ = CheckDevice();
    }

    public bool OwnKeyBilling => BillingMode.Length > 0 ? BillingMode == "own_key"
        : Engine.Status?.ClaudeAccess is { } a ? a != "credits" : KeyState is KeyStates.Ok or KeyStates.Bad;

    public async Task RefreshBilling()
    {
        if (!Profile.SetupDone || Credentials.Get(Credentials.Item.DeviceToken) is not { Length: > 0 } token) return;
        try
        {
            BillingMode = await HubClient.BillingMode(token);
            Prefs.Set("HubBilling", BillingMode);
            Emit();
        }
        catch (HubClient.SignedOut) { SignOut(); }
        catch { }
    }

    /// <summary>A computer revoked elsewhere goes back to first launch. Only a clear 403 counts.</summary>
    public async Task CheckDevice()
    {
        if (!Profile.SetupDone || Credentials.Get(Credentials.Item.DeviceToken) is not { Length: > 0 } token) return;
        try { await HubClient.CheckAccount(token); }
        catch (HubClient.SignedOut)
        {
            SignOut();
            Message = "This PC was disconnected from the congregation. Set it up again to publish meetings.";
            Emit();
        }
        catch { }
    }

    /// <summary>Forget the congregation on this computer. The details and the API key stay.</summary>
    public void SignOut()
    {
        Credentials.Delete(Credentials.Item.DeviceToken);
        OnboardingProgress.Clear();
        BillingMode = "";
        Prefs.Remove("HubBilling");
        Profile.Slug = "";
        Profile.Claim = "";
        Profile.SetupDone = false;
        Profile.Save();
        RelaunchEngine();
        SignedOutAction?.Invoke();
        Emit();
    }

    public void RelaunchEngine() => Engine.Restart(EngineEnvironment());

    private Dictionary<string, string> EngineEnvironment()
    {
        var env = new Dictionary<string, string> { ["HEARCHURCH_DEVICE_NAME"] = DeviceName };
        var v = typeof(AppModel).Assembly.GetName().Version;
        if (v != null)
        {
            env["HEARCHURCH_APP_VERSION"] = $"{v.Major}.{v.Minor}.{v.Build}";
            env["HEARCHURCH_APP_BUILD"] = v.Revision.ToString();
        }
        if (Credentials.Get(Credentials.Item.ApiKey) is { } key)
            env[HubClient.IsSubscriptionToken(key) ? "CLAUDE_CODE_OAUTH_TOKEN" : "ANTHROPIC_API_KEY"] = key;
        if (Profile.SetupDone && Credentials.Get(Credentials.Item.DeviceToken) is { Length: > 0 } token)
        {
            var hub = AppPaths.Hub;
            env["HEARCHURCH_HUB_URL"] = (hub.Scheme == "http" ? "ws" : "wss") + "://" + hub.Authority + "/ws/engine";
            env["HEARCHURCH_DEVICE_TOKEN"] = token;
        }
        if (ListenerUrl is { } url) env["HEARCHURCH_LISTENER_URL"] = url;
        env["HEARCHURCH_SHARE_LOGS"] = Profile.ShareLogs ? "1" : "0";
        return env;
    }

    public void SetShareLogs(bool on)
    {
        if (Profile.ShareLogs == on) return;
        Profile.ShareLogs = on;
        Profile.Save();
        _ = Engine.Set(new JsonObject { ["support"] = new JsonObject { ["share_logs"] = on } });
        Emit();
    }

    // ---------------------------------------------------------------- the meeting's languages and downloads

    public List<string> Speakers => Profile.Speakers;
    public List<string> Targets => Profile.Targets;

    public bool IsOn(string code, LanguageRole role) => (role == LanguageRole.Speaker ? Speakers : Targets).Contains(code);
    public PackageStatus? Package(string id) => Engine.Packages.FirstOrDefault(p => p.Id == id);
    public bool IsReady(string id) => Package(id)?.State == "ready";
    public bool IsReady(string code, LanguageRole role) => IsReady(OutputLanguage.PackageId(code, role));

    /// <summary>A toggle in a language grid. Returns why nothing changed, if so.</summary>
    public string? Toggle(string code, LanguageRole role)
    {
        var list = (role == LanguageRole.Speaker ? Profile.Speakers : Profile.Targets).ToList();
        if (list.Contains(code))
        {
            if (list.Count <= 1)
                return role == LanguageRole.Speaker ? "At least one language must be spoken." : "Keep at least one translation language.";
            list.Remove(code);
        }
        else
        {
            if (Running && !IsReady(code, role))
            {
                _ = Engine.Download(new[] { OutputLanguage.PackageId(code, role) });
                return $"{OutputLanguage.NameOf(code)} is downloading. Switch it on when it's ready.";
            }
            list.Add(code);
        }
        SetMeeting(role == LanguageRole.Speaker ? list : Profile.Speakers.ToList(), role == LanguageRole.Target ? list : Profile.Targets.ToList());
        return null;
    }

    public void SetMeeting(List<string> speakers, List<string> targets)
    {
        Profile.Speakers = speakers;
        Profile.Targets = targets;
        Profile.MeetingPicked = true;
        Profile.LanguagesPicked = true;
        Profile.Save();
        Emit();
        _ = Apply();

        async Task Apply()
        {
            if (Profile.SetupDone) await PushMeeting();
            await Engine.Download(NeededDownloads);
        }
    }

    private async Task PushMeeting()
    {
        var e = await Engine.Set(new JsonObject
        {
            ["meeting"] = new JsonObject
            {
                ["speakers"] = new JsonArray(Profile.Speakers.Select(s => (JsonNode)s!).ToArray()),
                ["targets"] = new JsonArray(Profile.Targets.Select(s => (JsonNode)s!).ToArray()),
            },
        });
        if (e != null) { Message = e; Emit(); }
    }

    public async Task PushChurch()
    {
        if (Profile.Church.Length == 0) return;
        await Engine.Set(new JsonObject { ["church"] = Profile.Church });
    }

    private void EngineStarted()
    {
        _post(async () =>
        {
            await PushChurch();
            if (Profile.SetupDone)
            {
                if (!Profile.MeetingPicked)
                {
                    await Engine.LoadSettings();
                    if (Engine.Settings?.Settings.Meeting is { } m && m.Speakers.Count > 0 && m.Targets.Count > 0)
                    {
                        Profile.Speakers = m.Speakers;
                        Profile.Targets = m.Targets;
                    }
                    Profile.MeetingPicked = true;
                    Profile.Save();
                }
                await PushMeeting();
            }
            if (Profile.SetupDone || Profile.LanguagesPicked) await Engine.Download(NeededDownloads);
            await DownloadEnglishOnce();
            StartMicPreview();
        });
    }

    /// <summary>English is downloaded once, at the first launch (it's in almost every meeting).</summary>
    private async Task DownloadEnglishOnce()
    {
        if (Prefs.GetBool("EnglishDownloadedOnce")) return;
        await Engine.RefreshPackages();
        if (Engine.Packages.Count == 0) return;
        Prefs.Set("EnglishDownloadedOnce", true);
        if (Package("en")?.State != "ready") await Engine.Download(new[] { "en" });
    }

    /// <summary>Someone changed the languages elsewhere (the speak page may): show what the engine does.</summary>
    private void FollowEngineMeeting(EngineStatus st)
    {
        if (!Profile.SetupDone || st.MeetingInfo is not { } m) return;
        if (_lastEngineMeeting != null && m.Speakers.SequenceEqual(_lastEngineMeeting.Speakers) && m.Targets.SequenceEqual(_lastEngineMeeting.Targets)) return;
        var first = _lastEngineMeeting == null;
        _lastEngineMeeting = m;
        if (first || m.Speakers.Count == 0 || m.Targets.Count == 0) return;
        if (m.Speakers.SequenceEqual(Profile.Speakers) && m.Targets.SequenceEqual(Profile.Targets)) return;
        Profile.Speakers = m.Speakers.ToList();
        Profile.Targets = m.Targets.ToList();
        Profile.Save();
    }

    /// <summary>Every language heard or spoken in the meeting is downloaded (speech recognition comes with any).</summary>
    public List<string> NeededDownloads
    {
        get
        {
            var o = Profile.Speakers.Select(s => OutputLanguage.PackageId(s, LanguageRole.Speaker)).ToList();
            foreach (var t in Profile.Targets) if (!o.Contains(t)) o.Add(t);
            return o;
        }
    }

    /// <summary>Downloads in progress (or failed), for the bar at the top of the window.</summary>
    public List<PackageStatus> Downloads => Engine.Packages.Where(p => p.State is "downloading" or "failed").ToList();

    /// <summary>The engine's starting meeting is the one chosen in setup.</summary>
    private void WriteMeeting()
    {
        var path = Path.Combine(AppPaths.Home, "data", "overrides.win32.json");
        JsonObject current;
        try { current = JsonNode.Parse(File.ReadAllText(path)) as JsonObject ?? new JsonObject(); } catch { current = new JsonObject(); }
        current["meeting"] = new JsonObject
        {
            ["speakers"] = new JsonArray(Profile.Speakers.Select(s => (JsonNode)s!).ToArray()),
            ["targets"] = new JsonArray(Profile.Targets.Select(s => (JsonNode)s!).ToArray()),
            ["auto_en"] = false,
        };
        current.Remove("languages");
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, current.ToJsonString(new System.Text.Json.JsonSerializerOptions { WriteIndented = true }));
        }
        catch { }
    }

    public void FinishSetup()
    {
        Profile.SetupDone = true;
        Profile.MeetingPicked = true;
        Profile.Approved = true;
        Profile.Save();
        WriteMeeting();
        RelaunchEngine();   // now with the device token, so meetings go to hearchurch.com
        SetupFinished?.Invoke();
        Emit();
    }

    /// <summary>Signed in: what the hub knows about the congregation.</summary>
    public void SignedIn(HubClient.Joined j)
    {
        Profile.Slug = j.Slug;
        if (j.Name.Length > 0) Profile.Name = j.Name;
        if (j.Church.Length > 0) Profile.Church = j.Church;
        if (j.UnitType.Length > 0) Profile.UnitType = j.UnitType;
        Profile.Stake = j.Stake;
        if (j.Address.Length > 0) Profile.Address = j.Address;
        Profile.Approved = true;
        Profile.Claim = "";
        Profile.Save();
        BillingMode = j.Billing;
        Prefs.Set("HubBilling", j.Billing);
        _ = PushChurch();
        Emit();
    }

    // ---------------------------------------------------------------- sound input

    public string SelectedInput => Engine.Settings?.Settings.Audio.InputDevice ?? "";

    /// <summary>The chosen input, if it has been unplugged.</summary>
    public string? MissingInput
    {
        get
        {
            var s = Engine.Settings;
            if (s == null || SelectedInput.Length == 0 || s.Devices.Any(d => d.Name == SelectedInput)) return null;
            return SelectedInput;
        }
    }

    private int _meterViews;
    private bool _windowVisible = true;

    public bool WindowVisible
    {
        get => _windowVisible;
        set { if (_windowVisible != value) { _windowVisible = value; PreviewConditionsChanged(); } }
    }

    public void MeterAppeared() { _meterViews++; PreviewConditionsChanged(); }
    public void MeterDisappeared() { _meterViews = Math.Max(0, _meterViews - 1); PreviewConditionsChanged(); }

    /// <summary>The microphone is open for the meter only while someone can see it.</summary>
    private bool PreviewWanted => _meterViews > 0 && _windowVisible;

    private void PreviewConditionsChanged()
    {
        if (PreviewWanted) StartMicPreview(); else Mic.Stop();
    }

    public void StartMicPreview()
    {
        if (Running || !PreviewWanted) return;
        Mic.Start(MissingInput == null ? SelectedInput : "");
    }

    public async Task ChooseInput(string name)
    {
        await Engine.Set(new JsonObject { ["audio"] = new JsonObject { ["input_device"] = name, ["input_channel"] = 0 } });
        Mic.Stop();
        StartMicPreview();
    }

    // ---------------------------------------------------------------- meeting

    /// <summary>`file`: a recording in the data folder's samples\ instead of the input (screenshots in CI).</summary>
    public async Task StartMeeting(string? file = null)
    {
        Message = null;
        Mic.Stop();   // the engine opens the input itself
        var body = new JsonObject { ["source"] = file == null ? "mic" : "file" };
        if (file != null) body["file"] = file;
        else if (Engine.Settings?.Settings.Audio.InputDevice is { Length: > 0 } dev) body["device"] = dev;
        var r = await Engine.Post("api/start", body);
        if (r.Ok) MeetingStarted = DateTime.UtcNow;
        else Message = r.Message.Length == 0 ? "Could not start." : r.Message;
        Emit();
    }

    public async Task StopMeeting()
    {
        await Engine.Post("api/stop");
        MeetingStarted = null;
        Emit();
    }

    // ---------------------------------------------------------------- checks

    public async Task<string?> SaveKey(string key)
    {
        key = key.Trim();
        if (!key.StartsWith("sk-ant-", StringComparison.Ordinal)) return "That doesn't look like an API key (they start with sk-ant-).";
        KeyState = KeyStates.Checking;
        Emit();
        var why = await HubClient.CheckApiKey(key);
        if (why != null)
        {
            KeyState = KeyStates.Bad;
            KeyProblem = why;
            Emit();
            return why;
        }
        if (!Credentials.Set(Credentials.Item.ApiKey, key))
        {
            KeyState = KeyStates.Missing;
            Emit();
            return "Could not save the key in Windows Credential Manager. Try again.";
        }
        KeyState = KeyStates.Ok;
        RelaunchEngine();
        Emit();
        return null;
    }

    public async Task CheckKey()
    {
        if (Credentials.Get(Credentials.Item.ApiKey) is not { } key) { KeyState = KeyStates.Missing; Emit(); return; }
        KeyState = KeyStates.Checking;
        var why = await HubClient.CheckApiKey(key);
        KeyState = why == null ? KeyStates.Ok : KeyStates.Bad;
        KeyProblem = why ?? "";
        Emit();
    }

    public async Task CheckHub()
    {
        _lastHubCheck = DateTime.UtcNow;
        var r = await HubClient.Reachable();
        if (r != HubReachable) { HubReachable = r; Emit(); }
    }

    /// <summary>Every 10 s: power, network and (less often) the hub.</summary>
    public void Tick()
    {
        var changed = false;
        if (WinSystem.OnBattery != OnBattery) { OnBattery = !OnBattery; changed = true; }
        if (WinSystem.Online != Online) { Online = !Online; changed = true; }
        if (Engine.Status == null && Engine.State != Engine.RunState.Running) WinSystem.KeepAwake(false);
        if ((DateTime.UtcNow - _lastHubCheck).TotalSeconds > (HubReachable == true ? 59 : 9)) _ = CheckHub();
        if (changed) Emit();
    }

    private void FollowRunning(EngineStatus st)
    {
        var running = st.Pipeline.Running;
        WinSystem.KeepAwake(running);
        if (running && MeetingStarted == null && st.Pipeline.StartedAt is { } t)
            MeetingStarted = DateTimeOffset.FromUnixTimeMilliseconds((long)(t * 1000)).UtcDateTime;
        else if (!running && MeetingStarted != null)
            MeetingStarted = null;
    }

    private void MicWarningChanged(EngineStatus st)
    {
        if (!st.Pipeline.Running || st.MicWarning is not { Length: > 0 } w) { _micWarningShown = false; return; }
        if (_micWarningShown) return;
        _micWarningShown = true;
        Notify?.Invoke("Hear Church: check the microphone", w);
    }

    public List<Check> Checks
    {
        get
        {
            var o = new List<Check>();
            var blocked = WinSystem.MicrophoneBlocked;
            o.Add(blocked
                ? new Check { Id = "mic", Title = "Microphone blocked", State = Check.Kind.Bad,
                              Detail = "Allow desktop apps in Settings → Privacy & security → Microphone.",
                              Action = ("Open Settings", () => Open("ms-settings:privacy-microphone")) }
                : new Check { Id = "mic", Title = "Microphone allowed", State = Check.Kind.Ok });
            if (!blocked && !Running)
            {
                if (MissingInput is { } missing)
                    o.Add(new Check { Id = "input", Title = $"{missing} is not connected", State = Check.Kind.Bad,
                                      Detail = "Connect it, or choose another input under Sound." });
                o.Add(Mic.HeardSound
                    ? new Check { Id = "level", Title = "Sound is reaching this PC", State = Check.Kind.Ok }
                    : new Check { Id = "level", Title = "No sound yet", State = Check.Kind.Warn,
                                  Detail = "Speak near the microphone, or check the sound system's cable." });
            }
            o.Add(OnBattery
                ? new Check { Id = "power", Title = "On battery", State = Check.Kind.Warn, Detail = "Plug the PC in and keep the lid open during the meeting." }
                : new Check { Id = "power", Title = "Plugged in", State = Check.Kind.Ok });
            var host = AppPaths.HubHost;
            if (!Online)
                o.Add(new Check { Id = "net", Title = "No internet", State = Check.Kind.Bad, Detail = "Connect to Wi-Fi or Ethernet." });
            else if (LocalSharing)
            {
                var h = ListenerUrl != null && Uri.TryCreate(ListenerUrl, UriKind.Absolute, out var u) ? u.Host : "";
                o.Add(h.Length == 0 || h == "127.0.0.1"
                    ? new Check { Id = "net", Title = "Not on a local network", State = Check.Kind.Bad,
                                  Detail = "Local sharing needs this PC on the same Wi-Fi as the phones." }
                    : new Check { Id = "net", Title = $"Sharing on this Wi-Fi ({h})", State = Check.Kind.Ok });
            }
            else if (HubReachable == false)
                o.Add(new Check { Id = "net", Title = $"Can't reach {host}", State = Check.Kind.Bad,
                                  Detail = "Listeners connect through it. Check the internet connection." });
            else
                o.Add(new Check { Id = "net", Title = "Internet connected", State = HubReachable == null ? Check.Kind.Pending : Check.Kind.Ok });

            var onCredits = Engine.Status != null ? Engine.Status.ClaudeAccess == "credits" : KeyState == KeyStates.Missing;
            if (onCredits)
                o.Add(LocalSharing && HubReachable == false
                    ? new Check { Id = "key", Title = "Translation on credit needs the internet", State = Check.Kind.Bad,
                                  Detail = $"Phones listen on this Wi-Fi, but the translation goes through {host}, which this PC can't reach. Check the internet connection." }
                    : new Check { Id = "key", Title = "Translation on Hear Church credit", State = Check.Kind.Ok });
            else
                o.Add(KeyState switch
                {
                    KeyStates.Ok => new Check { Id = "key", Title = "API key works", State = Check.Kind.Ok },
                    KeyStates.Unknown or KeyStates.Checking => new Check { Id = "key", Title = "Checking the API key…", State = Check.Kind.Pending },
                    KeyStates.Missing => new Check { Id = "key", Title = "No API key", State = Check.Kind.Bad, Detail = "Add one in Settings → AI.",
                                                     Action = ("Settings…", () => OpenSettings?.Invoke()) },
                    _ => new Check { Id = "key", Title = "API key problem", State = Check.Kind.Bad, Detail = KeyProblem,
                                     Action = ("Settings…", () => OpenSettings?.Invoke()) },
                });
            if (Profile.SetupDone && (Profile.Speakers.Count == 0 || Profile.Targets.Count == 0))
                o.Add(new Check { Id = "languages", Title = "No language chosen", State = Check.Kind.Bad,
                                  Detail = "Choose a speaker language and a translation language to start a meeting." });
            switch (Engine.State)
            {
                case Engine.RunState.Running:
                    var needed = NeededDownloads;
                    var pk = Engine.Packages.Where(p => p.Id == "speech" || needed.Contains(p.Id)).ToList();
                    var missingPk = pk.Where(p => p.State is "missing" or "failed").ToList();
                    if (pk.Any(p => p.State == "downloading"))
                        o.Add(new Check { Id = "models", Title = "Downloading languages…", State = Check.Kind.Pending });
                    else if (missingPk.Count > 0)
                        o.Add(new Check { Id = "models", Title = "Languages not downloaded", State = Check.Kind.Bad,
                                          Detail = string.Join(", ", missingPk.Select(p => p.Name)),
                                          Action = ("Download", () => _ = Engine.Download(NeededDownloads)) });
                    else if (pk.Count > 0)
                        o.Add(new Check { Id = "models", Title = "Languages downloaded", State = Check.Kind.Ok });
                    break;
                case Engine.RunState.Failed:
                    o.Add(new Check { Id = "engine", Title = "Translation engine stopped", State = Check.Kind.Bad, Detail = Engine.FailReason,
                                      Action = ("Try again", RelaunchEngine) });
                    break;
                default:
                    o.Add(new Check { Id = "engine", Title = "Starting the translation engine…", State = Check.Kind.Pending });
                    break;
            }
            return o;
        }
    }

    public bool CanStart => Engine.State == Engine.RunState.Running && !Checks.Any(c => c.State == Check.Kind.Bad);
    public int Problems => Checks.Count(c => c.State == Check.Kind.Bad);

    public static void Open(string url)
    {
        try { System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(url) { UseShellExecute = true }); } catch { }
    }
}

/// <summary>Where setup stopped, so quitting in the middle resumes there.</summary>
public static class OnboardingProgress
{
    public static string? Step
    {
        get => Prefs.GetString("OnboardingStep");
        set => Prefs.Set("OnboardingStep", value);
    }

    public static bool OwnKey
    {
        get => Prefs.GetBool("OnboardingOwnKey");
        set => Prefs.Set("OnboardingOwnKey", value);
    }

    public static void Clear()
    {
        Prefs.Remove("OnboardingStep");
        Prefs.Remove("OnboardingOwnKey");
    }
}
