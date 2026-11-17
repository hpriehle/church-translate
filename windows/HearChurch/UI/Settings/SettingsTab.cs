using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace HearChurch.UI.Settings;

/// <summary>One Settings tab: a title and sections, scrolling. Its parts are Slots, rebuilt only when what they show
/// changes, so a field being typed in isn't rebuilt under the operator.</summary>
public abstract class SettingsTab : ScrollViewer
{
    protected readonly AppModel Model;
    protected readonly StackPanel Body;
    private readonly List<Slot> _slots = new();

    protected SettingsTab(AppModel model, string title)
    {
        Model = model;
        Body = new StackPanel { Spacing = 22, Padding = new Thickness(32, 24, 32, 32), MaxWidth = 680, HorizontalAlignment = HorizontalAlignment.Stretch };
        Body.Children.Add(Ui.T(title, Ui.LargeTitle, bold: true));
        Content = Body;
        HorizontalScrollMode = ScrollMode.Disabled;
    }

    /// <summary>Adds a part that is rebuilt when key() changes.</summary>
    protected Slot Add(Func<string> key, Func<UIElement?> build)
    {
        var s = new Slot(key, build);
        _slots.Add(s);
        Body.Children.Add(s);
        s.Refresh();
        return s;
    }

    protected void Add(UIElement e) => Body.Children.Add(e);

    /// <summary>All of the tab, scrolled or not (the screenshots in CI).</summary>
    public UIElement Whole => Body;

    /// <summary>The model changed (the engine, downloads, billing…).</summary>
    public virtual void Refresh()
    {
        foreach (var s in _slots) s.Refresh();
    }

    /// <summary>Rebuild every part now (after a change made here).</summary>
    protected void Rebuild()
    {
        foreach (var s in _slots) s.Invalidate();
        Refresh();
    }

    /// <summary>Another tab opens, or the window closes.</summary>
    public virtual void Leaving() { }

    protected static string Hours(double h) => HearChurch.UI.Settings.Money.Hours(h);
}

public static class Money
{
    private static readonly System.Globalization.CultureInfo C = System.Globalization.CultureInfo.InvariantCulture;

    public static string Usd(double v) => v < 0 ? "-$" + (-v).ToString("0.00", C) : "$" + v.ToString("0.00", C);

    /// <summary>4 → "4", 1.5 → "1.5", 0.25 → "0.3".</summary>
    public static string Hours(double h)
    {
        var s = h.ToString("0.0", C);
        return s.EndsWith(".0") ? s[..^2] : s;
    }
}
