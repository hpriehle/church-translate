using System.ComponentModel;
using System.Diagnostics;

namespace HearChurch.Core;

/// <summary>"This Wi-Fi only": phones connect straight to the engine, which then listens on the network (0.0.0.0, on
/// the port the app picked at launch). Windows Firewall blocks that unless a rule lets it in. The port changes every
/// launch, so the rule is for the engine's program instead: inbound TCP to hearchurch-engine.exe, every network type
/// (a church Wi-Fi is usually "Public" on Windows). Added once, the first time the operator picks "This Wi-Fi only",
/// with netsh run elevated: Windows asks for permission (UAC) once. docs/windows-app.md.</summary>
public static class Firewall
{
    public const string RuleName = "Hear Church (This Wi-Fi only)";

    /// <summary>Is there already a rule for this engine (this install's path)? netsh's "show rule" needs no elevation.</summary>
    public static bool HasRule(string exe)
    {
        try
        {
            var psi = new ProcessStartInfo("netsh.exe")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            foreach (var a in new[] { "advfirewall", "firewall", "show", "rule", $"name={RuleName}", "verbose" }) psi.ArgumentList.Add(a);
            using var p = Process.Start(psi)!;
            var output = p.StandardOutput.ReadToEnd();
            p.WaitForExit(15000);
            return p.ExitCode == 0 && output.Contains(exe, StringComparison.OrdinalIgnoreCase);
        }
        catch { return false; }
    }

    /// <summary>The netsh arguments that add the rule (one string: the elevated start can't take an argument list).</summary>
    public static string AddRuleArguments(string exe) =>
        $"advfirewall firewall add rule name=\"{RuleName}\" dir=in action=allow protocol=TCP profile=any enable=yes " +
        $"program=\"{exe}\" description=\"Phones on this Wi-Fi reach Hear Church's translation (Settings: This Wi-Fi only).\"";

    /// <summary>Makes sure the rule is there; asks Windows (UAC) only if it isn't. Null when phones can connect, or what
    /// to tell the operator.</summary>
    public static async Task<string?> Allow()
    {
        if (Engine.EnginePath() is not { } exe) return null;   // a development build without the sidecar
        exe = Path.GetFullPath(exe);
        return await Task.Run(() =>
        {
            if (HasRule(exe)) return null;
            try
            {
                using var p = Process.Start(new ProcessStartInfo("netsh.exe", AddRuleArguments(exe))
                {
                    UseShellExecute = true,
                    Verb = "runas",
                    WindowStyle = ProcessWindowStyle.Hidden,
                })!;
                p.WaitForExit(60000);
                if (p.ExitCode == 0) return null;
            }
            catch (Win32Exception e) when (e.NativeErrorCode == 1223)   // the operator said No to Windows' question
            {
                return Declined;
            }
            catch { }
            return HasRule(exe) ? null : Declined;
        });
    }

    public const string Declined = "Windows Firewall may keep phones on this Wi-Fi from connecting to this PC. Choose This Wi-Fi only again and allow Hear Church when Windows asks.";
}
