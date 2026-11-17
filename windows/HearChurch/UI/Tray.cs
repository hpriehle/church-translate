using System.Runtime.InteropServices;

namespace HearChurch.UI;

/// <summary>The icon in the notification area (the Mac's menu bar item): it keeps the app and the engine running when
/// the window is closed. Left click opens the window; right click: Start/Stop, Open, Start at login, Check for Updates, Quit.
/// Plain Shell_NotifyIcon on a message-only window, so there is no extra dependency.</summary>
public sealed class Tray : IDisposable
{
    public sealed record Item(string Text, Action? Run, bool Checked = false, bool Enabled = true);

    private readonly Func<List<Item?>> _menu;   // null = separator
    private readonly Action _open;
    private readonly WndProc _proc;
    private readonly IntPtr _hwnd, _icon;
    private readonly uint _taskbarCreated;
    private string _tip = "Hear Church";
    private const uint WM_TRAY = 0x8001;   // WM_APP + 1
    private bool _added;

    public Tray(string iconPath, Action open, Func<List<Item?>> menu)
    {
        _open = open;
        _menu = menu;
        _proc = Proc;
        var cls = new WNDCLASSEX
        {
            cbSize = (uint)Marshal.SizeOf<WNDCLASSEX>(),
            lpfnWndProc = Marshal.GetFunctionPointerForDelegate(_proc),
            hInstance = GetModuleHandle(null),
            lpszClassName = "HearChurchTray",
        };
        RegisterClassEx(ref cls);
        _hwnd = CreateWindowEx(0, "HearChurchTray", "Hear Church", 0, 0, 0, 0, 0, IntPtr.Zero, IntPtr.Zero, cls.hInstance, IntPtr.Zero);
        _icon = LoadImage(IntPtr.Zero, iconPath, 1 /* IMAGE_ICON */, GetSystemMetrics(49), GetSystemMetrics(50), 0x10 /* LR_LOADFROMFILE */);
        _taskbarCreated = RegisterWindowMessage("TaskbarCreated");
        Add();
    }

    private void Add()
    {
        var d = Data();
        _added = Shell_NotifyIcon(0 /* NIM_ADD */, ref d);
    }

    private NOTIFYICONDATA Data(uint flags = 0x1 | 0x2 | 0x4)   // NIF_MESSAGE | NIF_ICON | NIF_TIP
    {
        return new NOTIFYICONDATA
        {
            cbSize = (uint)Marshal.SizeOf<NOTIFYICONDATA>(),
            hWnd = _hwnd,
            uID = 1,
            uFlags = flags,
            uCallbackMessage = WM_TRAY,
            hIcon = _icon,
            szTip = _tip,
            szInfo = "",
            szInfoTitle = "",
        };
    }

    public void SetTip(string tip)
    {
        if (tip == _tip) return;
        _tip = tip;
        var d = Data(0x4);
        Shell_NotifyIcon(1 /* NIM_MODIFY */, ref d);
    }

    /// <summary>A balloon / toast from the icon (the microphone warning during a meeting).</summary>
    public void Notify(string title, string text, bool warning = true)
    {
        var d = Data(0x10);   // NIF_INFO
        d.szInfoTitle = title.Length > 63 ? title[..63] : title;
        d.szInfo = text.Length > 255 ? text[..255] : text;
        d.dwInfoFlags = warning ? 0x2u : 0x1u;  // NIIF_WARNING, NIIF_INFO
        Shell_NotifyIcon(1, ref d);
    }

    private IntPtr Proc(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam)
    {
        if (msg == WM_TRAY)
        {
            var ev = (uint)lParam.ToInt64() & 0xFFFF;
            if (ev is 0x0202 or 0x0203) _open();               // WM_LBUTTONUP, WM_LBUTTONDBLCLK
            else if (ev == 0x0205) ShowMenu();                  // WM_RBUTTONUP
            return IntPtr.Zero;
        }
        if (msg == _taskbarCreated && _taskbarCreated != 0) { Add(); return IntPtr.Zero; }   // Explorer restarted
        return DefWindowProc(hwnd, msg, wParam, lParam);
    }

    private void ShowMenu()
    {
        var items = _menu();
        var menu = CreatePopupMenu();
        for (var i = 0; i < items.Count; i++)
        {
            var it = items[i];
            if (it == null) { AppendMenu(menu, 0x800 /* MF_SEPARATOR */, UIntPtr.Zero, null); continue; }
            uint flags = (it.Checked ? 0x8u : 0) | (it.Enabled && it.Run != null ? 0u : 0x1u /* MF_GRAYED */);
            AppendMenu(menu, flags, (UIntPtr)(i + 1), it.Text);
        }
        GetCursorPos(out var pt);
        SetForegroundWindow(_hwnd);
        var cmd = TrackPopupMenuEx(menu, 0x100 /* TPM_RETURNCMD */ | 0x2 /* TPM_RIGHTBUTTON */, pt.X, pt.Y, _hwnd, IntPtr.Zero);
        DestroyMenu(menu);
        if (cmd > 0 && cmd <= items.Count) items[cmd - 1]?.Run?.Invoke();
    }

    public void Dispose()
    {
        if (_added)
        {
            var d = Data();
            Shell_NotifyIcon(2 /* NIM_DELETE */, ref d);
            _added = false;
        }
        if (_hwnd != IntPtr.Zero) DestroyWindow(_hwnd);
    }

    // ---------------------------------------------------------------- Win32

    private delegate IntPtr WndProc(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WNDCLASSEX
    {
        public uint cbSize, style;
        public IntPtr lpfnWndProc;
        public int cbClsExtra, cbWndExtra;
        public IntPtr hInstance, hIcon, hCursor, hbrBackground;
        public string? lpszMenuName;
        public string lpszClassName;
        public IntPtr hIconSm;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NOTIFYICONDATA
    {
        public uint cbSize;
        public IntPtr hWnd;
        public uint uID, uFlags, uCallbackMessage;
        public IntPtr hIcon;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string szTip;
        public uint dwState, dwStateMask;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string szInfo;
        public uint uVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string szInfoTitle;
        public uint dwInfoFlags;
        public Guid guidItem;
        public IntPtr hBalloonIcon;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT { public int X, Y; }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)] private static extern bool Shell_NotifyIcon(uint msg, ref NOTIFYICONDATA data);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern ushort RegisterClassEx(ref WNDCLASSEX cls);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateWindowEx(uint exStyle, string cls, string name, uint style, int x, int y, int w, int h, IntPtr parent, IntPtr menu, IntPtr inst, IntPtr param);
    [DllImport("user32.dll")] private static extern bool DestroyWindow(IntPtr hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr DefWindowProc(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern uint RegisterWindowMessage(string name);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr LoadImage(IntPtr inst, string name, uint type, int cx, int cy, uint load);
    [DllImport("user32.dll")] private static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll")] private static extern IntPtr CreatePopupMenu();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern bool AppendMenu(IntPtr menu, uint flags, UIntPtr id, string? text);
    [DllImport("user32.dll")] private static extern int TrackPopupMenuEx(IntPtr menu, uint flags, int x, int y, IntPtr hwnd, IntPtr tpm);
    [DllImport("user32.dll")] private static extern bool DestroyMenu(IntPtr menu);
    [DllImport("user32.dll")] private static extern bool GetCursorPos(out POINT pt);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr GetModuleHandle(string? name);
}
