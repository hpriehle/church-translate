using System.Runtime.InteropServices.WindowsRuntime;
using Microsoft.UI.Text;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.Foundation;
using Windows.Storage.Streams;

namespace HearChurch.UI;

/// <summary>Small builders so the views read like the Mac's SwiftUI: text, colours from the Windows theme, sections.</summary>
public static class Ui
{
    public static Brush Res(string key) => (Brush)Application.Current.Resources[key];
    public static Brush Primary => Res("TextFillColorPrimaryBrush");
    public static Brush Secondary => Res("TextFillColorSecondaryBrush");
    public static Brush Tertiary => Res("TextFillColorTertiaryBrush");
    public static Brush Red => Res("SystemFillColorCriticalBrush");
    public static Brush Green => Res("SystemFillColorSuccessBrush");
    public static Brush Orange => Res("SystemFillColorCautionBrush");
    public static Brush Accent => Res("AccentTextFillColorPrimaryBrush");
    public static Brush DividerBrush => Res("DividerStrokeColorDefaultBrush");
    public static Brush Layer => Res("LayerFillColorDefaultBrush");
    public static Brush Card => Res("CardBackgroundFillColorDefaultBrush");
    public static Brush WindowBackground => Res("SolidBackgroundFillColorBaseBrush");
    public static Brush Subtle => Res("SubtleFillColorSecondaryBrush");

    public const double Caption = 12, Body = 14, Callout = 13, Title3 = 20, LargeTitle = 28;

    public static TextBlock T(string text, double size = Body, Brush? color = null, bool bold = false, bool semibold = false,
                              bool wrap = true, bool select = false)
    {
        var t = new TextBlock
        {
            Text = text,
            FontSize = size,
            TextWrapping = wrap ? TextWrapping.Wrap : TextWrapping.NoWrap,
            TextTrimming = wrap ? TextTrimming.None : TextTrimming.CharacterEllipsis,
            IsTextSelectionEnabled = select,
        };
        if (color != null) t.Foreground = color;
        if (bold) t.FontWeight = FontWeights.Bold;
        else if (semibold) t.FontWeight = FontWeights.SemiBold;
        return t;
    }

    public static FontIcon Icon(string glyph, double size = 14, Brush? color = null)
    {
        var i = new FontIcon { Glyph = glyph, FontSize = size };
        if (color != null) i.Foreground = color;
        return i;
    }

    /// <summary>An icon and a text side by side (SwiftUI's Label).</summary>
    public static StackPanel Label(string text, string glyph, Brush? color = null, double size = Body)
    {
        var p = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6 };
        p.Children.Add(Icon(glyph, size, color));
        p.Children.Add(T(text, size, color, wrap: false));
        return p;
    }

    public static StackPanel Row(double spacing, params UIElement[] children)
    {
        var p = new StackPanel { Orientation = Orientation.Horizontal, Spacing = spacing };
        foreach (var c in children) p.Children.Add(c);
        return p;
    }

    public static StackPanel Column(double spacing, params UIElement[] children)
    {
        var p = new StackPanel { Spacing = spacing };
        foreach (var c in children) p.Children.Add(c);
        return p;
    }

    public static Border Divider(bool vertical = false) => vertical
        ? new Border { Width = 1, Background = DividerBrush, VerticalAlignment = VerticalAlignment.Stretch }
        : new Border { Height = 1, Background = DividerBrush, HorizontalAlignment = HorizontalAlignment.Stretch };

    /// <summary>A sidebar section: a small uppercase title, then its content.</summary>
    public static StackPanel Section(string title, params UIElement[] content)
    {
        var p = new StackPanel { Spacing = 10 };
        p.Children.Add(T(title.ToUpperInvariant(), Caption, Secondary, semibold: true));
        foreach (var c in content) p.Children.Add(c);
        return p;
    }

    /// <summary>A Settings section (SwiftUI's grouped Form Section): a header, its rows on a card, a small footer.</summary>
    public static StackPanel Group(string? header, string? footer, params UIElement?[] rows)
    {
        var p = new StackPanel { Spacing = 6 };
        if (!string.IsNullOrEmpty(header)) p.Children.Add(T(header, Body, semibold: true));
        var card = new StackPanel { Spacing = 12 };
        foreach (var r in rows) if (r != null) card.Children.Add(r);
        p.Children.Add(new Border
        {
            Background = Card,
            BorderBrush = Res("CardStrokeColorDefaultBrush"),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(16, 12, 16, 12),
            Child = card,
        });
        if (!string.IsNullOrEmpty(footer)) p.Children.Add(T(footer, Caption, Secondary));
        return p;
    }

    /// <summary>Something on the left, something on the right (SwiftUI's LabeledContent / HStack with a Spacer).</summary>
    public static Grid Field(UIElement left, UIElement? right)
    {
        var g = new Grid { ColumnSpacing = 12 };
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        if (left is FrameworkElement fl) fl.VerticalAlignment = VerticalAlignment.Center;
        g.Children.Add(left);
        if (right != null)
        {
            if (right is FrameworkElement fr) fr.VerticalAlignment = VerticalAlignment.Center;
            Grid.SetColumn((FrameworkElement)right, 1);
            g.Children.Add(right);
        }
        return g;
    }

    public static Grid Field(string label, string value) => Field(T(label), T(value, Body, Secondary, select: true));

    public static ProgressRing Busy() => new() { IsActive = true, Width = 16, Height = 16 };

    /// <summary>A question with one action and Cancel (SwiftUI's confirmationDialog / alert). True if the action was chosen.</summary>
    public static async Task<bool> Confirm(XamlRoot? root, string title, string message, string action)
    {
        if (root == null) return false;
        var dlg = new ContentDialog
        {
            Title = title,
            Content = T(message),
            PrimaryButtonText = action,
            CloseButtonText = "Cancel",
            DefaultButton = ContentDialogButton.Close,
            XamlRoot = root,
        };
        try { return await dlg.ShowAsync() == ContentDialogResult.Primary; }
        catch { return false; }   // another dialog is open
    }

    public static void Tip(DependencyObject el, string? text)
    {
        if (!string.IsNullOrEmpty(text)) ToolTipService.SetToolTip(el, text);
    }

    public static Button Button(string text, Action click, bool accent = false)
    {
        var b = new Button { Content = text };
        if (accent) b.Style = (Style)Application.Current.Resources["AccentButtonStyle"];
        b.Click += (_, _) => click();
        return b;
    }

    public static HyperlinkButton Link(string text, Action click)
    {
        var b = new HyperlinkButton { Content = text, Padding = new Thickness(0) };
        b.Click += (_, _) => click();
        return b;
    }

    public static HyperlinkButton Link(string text, string url) =>
        new() { Content = text, NavigateUri = new Uri(url), Padding = new Thickness(0) };

    /// <summary>A QR code for the listener link (QRCoder, error correction M as on the Mac), on white.</summary>
    public static Border Qr(string text, double size)
    {
        var img = new Image { Width = size, Height = size, Stretch = Stretch.Uniform };
        _ = Load();
        return new Border { Child = img, Background = new SolidColorBrush(Microsoft.UI.Colors.White), Padding = new Thickness(6), CornerRadius = new CornerRadius(6) };

        async Task Load()
        {
            try
            {
                using var gen = new QRCoder.QRCodeGenerator();
                using var data = gen.CreateQrCode(text, QRCoder.QRCodeGenerator.ECCLevel.M);
                var png = new QRCoder.PngByteQRCode(data).GetGraphic(8);
                using var ms = new InMemoryRandomAccessStream();
                await ms.WriteAsync(png.AsBuffer());
                ms.Seek(0);
                var bmp = new BitmapImage();
                await bmp.SetSourceAsync(ms);
                img.Source = bmp;
            }
            catch { }
        }
    }
}

/// <summary>A place in a view rebuilt only when what it shows changes (its key): controls the operator is using are
/// not rebuilt under them every second.</summary>
public sealed class Slot : ContentControl
{
    private string? _key;
    private readonly Func<string> _keyOf;
    private readonly Func<UIElement?> _build;

    public Slot(Func<string> key, Func<UIElement?> build)
    {
        _keyOf = key;
        _build = build;
        HorizontalContentAlignment = HorizontalAlignment.Stretch;
        VerticalContentAlignment = VerticalAlignment.Stretch;
        HorizontalAlignment = HorizontalAlignment.Stretch;
        IsTabStop = false;
    }

    public void Refresh()
    {
        var k = _keyOf();
        if (k == _key) return;
        _key = k;
        Content = _build();
    }

    public void Invalidate() => _key = null;
}

/// <summary>The input level, -60…0 dB: green, red near the top.</summary>
public sealed class LevelMeter : Grid
{
    private readonly Border _fill;
    private readonly double _width;

    public LevelMeter(double width)
    {
        _width = width;
        Width = width;
        Height = 8;
        VerticalAlignment = VerticalAlignment.Center;
        Children.Add(new Border { Background = Ui.Subtle, CornerRadius = new CornerRadius(4) });
        _fill = new Border { CornerRadius = new CornerRadius(4), HorizontalAlignment = HorizontalAlignment.Left, Width = 0, Background = Ui.Green };
        Children.Add(_fill);
        AutomationProperties_SetName(this, "Input level");
    }

    private static void AutomationProperties_SetName(DependencyObject d, string name) =>
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(d, name);

    public void SetDb(double db)
    {
        var frac = Math.Max(0, Math.Min(1, (db + 60) / 60));
        _fill.Width = _width * frac;
        _fill.Background = frac > 0.9 ? Ui.Red : Ui.Green;
    }
}

/// <summary>Equal-width cells, as many per row as fit at MinCellWidth, every row centered (a part-filled last row too).</summary>
public sealed class CenteredGrid : Panel
{
    public double MinCellWidth { get; set; } = 128;
    public double Spacing { get; set; } = 8;
    public bool Center { get; set; } = true;

    private (int Cols, double Cell) Columns(double width)
    {
        var cols = Math.Max(1, (int)((width + Spacing) / (MinCellWidth + Spacing)));
        return (cols, Math.Max(MinCellWidth, (width - (cols - 1) * Spacing) / cols));
    }

    private List<double> RowHeights(int cols, double cell)
    {
        var rows = new List<double>();
        for (var start = 0; start < Children.Count; start += cols)
        {
            double h = 0;
            for (var i = start; i < Math.Min(start + cols, Children.Count); i++)
            {
                Children[i].Measure(new Size(cell, double.PositiveInfinity));
                h = Math.Max(h, Children[i].DesiredSize.Height);
            }
            rows.Add(h);
        }
        return rows;
    }

    protected override Size MeasureOverride(Size available)
    {
        var width = double.IsInfinity(available.Width) ? MinCellWidth * 3 + Spacing * 2 : available.Width;
        var (cols, cell) = Columns(width);
        var rows = RowHeights(cols, cell);
        return new Size(width, rows.Sum() + Math.Max(0, rows.Count - 1) * Spacing);
    }

    protected override Size ArrangeOverride(Size final)
    {
        var (cols, cell) = Columns(final.Width);
        var y = 0.0;
        var rows = RowHeights(cols, cell);
        for (var r = 0; r < rows.Count; r++)
        {
            int start = r * cols, end = Math.Min(start + cols, Children.Count);
            var used = (end - start) * cell + (end - start - 1) * Spacing;
            var x = Center ? (final.Width - used) / 2 : 0;
            for (var i = start; i < end; i++)
            {
                Children[i].Arrange(new Rect(x, y, cell, rows[r]));
                x += cell + Spacing;
            }
            y += rows[r] + Spacing;
        }
        return final;
    }
}
