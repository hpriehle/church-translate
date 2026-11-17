using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32;

namespace HearChurch.Core;

/// <summary>Secrets in Windows Credential Manager (the Mac's Keychain): the device token and the API key. They are
/// passed to the engine in its environment and never written to disk by the app.</summary>
public static class Credentials
{
    public enum Item { DeviceToken, ApiKey }

    private static string Target(Item item) => "HearChurch:" + item switch { Item.DeviceToken => "deviceToken", _ => "apiKey" };

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct CREDENTIAL
    {
        public uint Flags;
        public uint Type;
        public string TargetName;
        public string? Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public string? TargetAlias;
        public string UserName;
    }

    private const uint CRED_TYPE_GENERIC = 1, CRED_PERSIST_LOCAL_MACHINE = 2;

    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredWrite(ref CREDENTIAL credential, uint flags);

    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredRead(string target, uint type, uint flags, out IntPtr credential);

    [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredDelete(string target, uint type, uint flags);

    [DllImport("advapi32.dll")]
    private static extern void CredFree(IntPtr buffer);

    public static string? Get(Item item)
    {
        try
        {
            if (!CredRead(Target(item), CRED_TYPE_GENERIC, 0, out var ptr)) return null;
            try
            {
                var c = Marshal.PtrToStructure<CREDENTIAL>(ptr);
                if (c.CredentialBlobSize == 0) return null;
                var bytes = new byte[c.CredentialBlobSize];
                Marshal.Copy(c.CredentialBlob, bytes, 0, bytes.Length);
                var s = Encoding.UTF8.GetString(bytes);
                return s.Length == 0 ? null : s;
            }
            finally { CredFree(ptr); }
        }
        catch { return null; }
    }

    public static bool Set(Item item, string value)
    {
        var bytes = Encoding.UTF8.GetBytes(value);
        var blob = Marshal.AllocHGlobal(bytes.Length);
        try
        {
            Marshal.Copy(bytes, 0, blob, bytes.Length);
            var c = new CREDENTIAL
            {
                Type = CRED_TYPE_GENERIC,
                TargetName = Target(item),
                CredentialBlobSize = (uint)bytes.Length,
                CredentialBlob = blob,
                Persist = CRED_PERSIST_LOCAL_MACHINE,
                UserName = "Hear Church",
            };
            return CredWrite(ref c, 0);
        }
        catch { return false; }
        finally { Marshal.FreeHGlobal(blob); }
    }

    public static void Delete(Item item)
    {
        try { CredDelete(Target(item), CRED_TYPE_GENERIC, 0); } catch { }
    }
}

/// <summary>"Open Hear Church when this PC starts": HKCU\…\Run (no admin needed). Started that way, the app opens in
/// the notification area only (--background).</summary>
public static class LoginItem
{
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string Name = "HearChurch";

    private static string Command => $"\"{Environment.ProcessPath}\" --background";

    public static bool Enabled
    {
        get
        {
            try
            {
                using var k = Registry.CurrentUser.OpenSubKey(RunKey);
                return k?.GetValue(Name) is string s && s.Length > 0;
            }
            catch { return false; }
        }
    }

    public static void Set(bool on)
    {
        try
        {
            using var k = Registry.CurrentUser.CreateSubKey(RunKey, true);
            if (on) k.SetValue(Name, Command);
            else k.DeleteValue(Name, false);
        }
        catch { }
    }
}

/// <summary>Power, sleep and the microphone privacy switch.</summary>
public static class WinSystem
{
    [StructLayout(LayoutKind.Sequential)]
    private struct SYSTEM_POWER_STATUS
    {
        public byte ACLineStatus, BatteryFlag, BatteryLifePercent, SystemStatusFlag;
        public int BatteryLifeTime, BatteryFullLifeTime;
    }

    [DllImport("kernel32.dll")]
    private static extern bool GetSystemPowerStatus(out SYSTEM_POWER_STATUS status);

    [DllImport("kernel32.dll")]
    private static extern uint SetThreadExecutionState(uint flags);

    private const uint ES_CONTINUOUS = 0x80000000, ES_SYSTEM_REQUIRED = 0x1, ES_DISPLAY_REQUIRED = 0x2;

    /// <summary>A laptop running on its battery (desktops report "online" or unknown).</summary>
    public static bool OnBattery
    {
        get
        {
            try { return GetSystemPowerStatus(out var s) && s.ACLineStatus == 0; } catch { return false; }
        }
    }

    private static bool _awake;

    /// <summary>Keep the PC (and its screen) awake while a meeting runs. Call on the UI thread (it is per thread).</summary>
    public static void KeepAwake(bool on)
    {
        if (on == _awake) return;
        _awake = on;
        try { SetThreadExecutionState(on ? ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED : ES_CONTINUOUS); } catch { }
    }

    /// <summary>Settings → Privacy & security → Microphone: off for this user or for desktop apps.</summary>
    public static bool MicrophoneBlocked
    {
        get
        {
            const string store = @"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone";
            try
            {
                foreach (var (root, path) in new[] { (Registry.LocalMachine, store), (Registry.CurrentUser, store),
                                                     (Registry.CurrentUser, store + @"\NonPackaged") })
                {
                    using var k = root.OpenSubKey(path);
                    if (k?.GetValue("Value") is string v && v.Equals("Deny", StringComparison.OrdinalIgnoreCase)) return true;
                }
            }
            catch { }
            return false;
        }
    }

    public static bool Online => System.Net.NetworkInformation.NetworkInterface.GetIsNetworkAvailable();
}
