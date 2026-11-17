using System.Runtime.InteropServices;
using HearChurch.Core;
using Microsoft.UI.Dispatching;
using Velopack;

namespace HearChurch;

/// <summary>The app's own Main (DISABLE_XAML_GENERATED_MAIN in the .csproj) so Velopack runs before any window: the
/// installer, the updater and the uninstaller start HearChurch.exe with their hook arguments, and Run() handles those
/// and exits. Velopack's own "apply a downloaded update at startup" is off: a second start (the shortcut clicked while
/// Hear Church runs) would stop the running copy, meeting and all. App.OnLaunched applies it instead, only in the first
/// copy (docs/windows-release.md).</summary>
public static class Program
{
    [DllImport("Microsoft.ui.xaml.dll")]
    private static extern void XamlCheckProcessRequirements();

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            VelopackApp.Build()
                .SetAutoApplyOnStartup(false)
                .OnBeforeUninstallFastCallback(_ => Updates.BeforeUninstall())
                .Run();
        }
        catch (Exception e)
        {
            Updates.Log($"startup: {e.Message}");
        }

        XamlCheckProcessRequirements();
        WinRT.ComWrappersSupport.InitializeComWrappers();
        Microsoft.UI.Xaml.Application.Start(_ =>
        {
            SynchronizationContext.SetSynchronizationContext(new DispatcherQueueSynchronizationContext(DispatcherQueue.GetForCurrentThread()));
            _ = new App();
        });
        return 0;
    }
}
