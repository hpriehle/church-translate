using NAudio.CoreAudioApi;
using NAudio.CoreAudioApi.Interfaces;
using NAudio.Wave;

namespace HearChurch.Core;

/// <summary>The level meter before a meeting (the Mac's MicMonitor): opens the chosen input with WASAPI only while the
/// meter can be seen. During a meeting the engine has the input and reports its level itself.
/// The engine names inputs as PortAudio (MME) does, which cuts names at 31 characters: matched by prefix.</summary>
public sealed class MicMonitor : IDisposable
{
    public double Db { get; private set; } = -80;
    /// <summary>Something louder than -50 dB in the last ~10 s.</summary>
    public bool HeardSound { get; private set; }
    public event Action? LevelChanged;
    public event Action? HeardChanged;
    public Action? OnDevicesChanged;

    private readonly Action<Action> _post;
    private WasapiCapture? _capture;
    private string _device = "";
    private readonly Queue<(DateTime At, double Db)> _history = new();
    private DateTime _lastShown = DateTime.MinValue;
    private MMDeviceEnumerator? _enumerator;
    private Notifications? _notifications;

    // Core Audio objects live on a worker (MTA) thread: NAudio's WASAPI calls fail on the UI's STA thread.
    private Task _queue = Task.CompletedTask;
    private readonly object _gate = new();
    private volatile bool _open;

    private void OnWorker(Action a)
    {
        lock (_gate) { _queue = _queue.ContinueWith(_ => { try { a(); } catch { } }, TaskScheduler.Default); }
    }

    public MicMonitor(Action<Action> post)
    {
        _post = post;
        OnWorker(() =>
        {
            _enumerator = new MMDeviceEnumerator();
            _notifications = new Notifications(() => _post(() => OnDevicesChanged?.Invoke()));
            _enumerator.RegisterEndpointNotificationCallback(_notifications);
        });
    }

    public bool Running => _open;

    /// <summary>"" = the default input.</summary>
    public void Start(string device)
    {
        if (_open && device == _device) return;
        _device = device;
        _open = true;
        OnWorker(() =>
        {
            Close();
            if (!_open) return;
            var dev = Find(device);
            if (dev == null) return;
            var cap = new WasapiCapture(dev, true, 50);
            cap.DataAvailable += (_, e) => Measure(cap.WaveFormat, e.Buffer, e.BytesRecorded);
            cap.StartRecording();
            _capture = cap;
        });
    }

    public void Stop()
    {
        _open = false;
        OnWorker(Close);
        Db = -80;
        _post(() => LevelChanged?.Invoke());
    }

    private void Close()
    {
        var c = _capture;
        _capture = null;
        if (c == null) return;
        try { c.StopRecording(); } catch { }
        try { c.Dispose(); } catch { }
    }

    private MMDevice? Find(string name)
    {
        if (_enumerator == null) return null;
        if (string.IsNullOrEmpty(name))
        {
            try { return _enumerator.GetDefaultAudioEndpoint(DataFlow.Capture, Role.Multimedia); } catch { return null; }
        }
        var all = _enumerator.EnumerateAudioEndPoints(DataFlow.Capture, DeviceState.Active).ToList();
        return all.FirstOrDefault(d => d.FriendlyName == name)
            ?? all.FirstOrDefault(d => d.FriendlyName.StartsWith(name, StringComparison.OrdinalIgnoreCase))
            ?? all.FirstOrDefault(d => name.StartsWith(d.FriendlyName, StringComparison.OrdinalIgnoreCase));
    }

    private void Measure(WaveFormat f, byte[] buf, int n)
    {
        double peak = 0;
        if (f.Encoding == WaveFormatEncoding.IeeeFloat || (f.Encoding == WaveFormatEncoding.Extensible && f.BitsPerSample == 32))
        {
            for (var i = 0; i + 4 <= n; i += 4) peak = Math.Max(peak, Math.Abs(BitConverter.ToSingle(buf, i)));
        }
        else if (f.BitsPerSample == 16)
        {
            for (var i = 0; i + 2 <= n; i += 2) peak = Math.Max(peak, Math.Abs(BitConverter.ToInt16(buf, i) / 32768.0));
        }
        var db = peak > 0 ? Math.Max(-80, 20 * Math.Log10(peak)) : -80;
        var now = DateTime.UtcNow;
        if ((now - _lastShown).TotalMilliseconds < 90) return;   // ~10 updates a second is plenty for a meter
        _lastShown = now;
        _post(() =>
        {
            if (!_open) return;
            Db = db;
            _history.Enqueue((now, db));
            while (_history.Count > 0 && (now - _history.Peek().At).TotalSeconds > 10) _history.Dequeue();
            var heard = _history.Any(h => h.Db > -50);
            if (heard != HeardSound) { HeardSound = heard; HeardChanged?.Invoke(); }
            LevelChanged?.Invoke();
        });
    }

    public void Dispose()
    {
        _open = false;
        OnWorker(() =>
        {
            Close();
            if (_notifications != null) _enumerator?.UnregisterEndpointNotificationCallback(_notifications);
            _enumerator?.Dispose();
        });
        try { _queue.Wait(2000); } catch { }
    }

    private sealed class Notifications : IMMNotificationClient
    {
        private readonly Action _changed;
        public Notifications(Action changed) { _changed = changed; }
        public void OnDeviceStateChanged(string deviceId, DeviceState newState) => _changed();
        public void OnDeviceAdded(string pwstrDeviceId) => _changed();
        public void OnDeviceRemoved(string deviceId) => _changed();
        public void OnDefaultDeviceChanged(DataFlow flow, Role role, string defaultDeviceId) { if (flow == DataFlow.Capture) _changed(); }
        public void OnPropertyValueChanged(string pwstrDeviceId, PropertyKey key) { }
    }
}
