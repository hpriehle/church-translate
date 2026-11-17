using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Media;

namespace HearChurch.UI;

/// <summary>What is said, in columns: the original (as spoken) and one per translation language, a row per sentence,
/// newest at the top; each column says how far behind the speaker it is. The Mac's TranscriptView.</summary>
public sealed class TranscriptView : Grid
{
    private readonly AppModel _model;
    private readonly TextBlock _summary;
    private readonly ToggleButton _activity;
    private readonly Grid _body = new();
    private readonly ListView _log = new() { Height = 140, SelectionMode = ListViewSelectionMode.None, Visibility = Visibility.Collapsed };
    private readonly StackPanel _rows = new();
    private readonly Grid _header = new();
    private readonly Dictionary<int, (string Key, UIElement Row)> _rowCache = new();
    private string _columnsKey = "";
    private double _width = 200;
    private string _logKey = "";
    private const double MinColumnWidth = 200;

    public TranscriptView(AppModel model, string title = "Transcript")
    {
        _model = model;
        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

        var bar = new Grid { Padding = new Thickness(16, 10, 16, 10) };
        bar.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        bar.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        bar.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var t = Ui.T(title, 15, semibold: true, wrap: false);
        t.VerticalAlignment = VerticalAlignment.Center;
        bar.Children.Add(t);
        _summary = Ui.T("", Ui.Callout, Ui.Secondary, wrap: false);
        _summary.Margin = new Thickness(10, 0, 0, 0);
        _summary.VerticalAlignment = VerticalAlignment.Center;
        SetColumn(_summary, 1);
        bar.Children.Add(_summary);
        _activity = new ToggleButton { Content = "Activity", Padding = new Thickness(10, 3, 10, 3) };
        _activity.Click += (_, _) => { _log.Visibility = _activity.IsChecked == true ? Visibility.Visible : Visibility.Collapsed; RefreshLog(); };
        SetColumn(_activity, 2);
        bar.Children.Add(_activity);
        Children.Add(bar);

        var d1 = Ui.Divider();
        SetRow(d1, 1);
        Children.Add(d1);
        SetRow(_body, 2);
        Children.Add(_body);
        var logPanel = new StackPanel();
        logPanel.Children.Add(Ui.Divider());
        logPanel.Children.Add(_log);
        SetRow(logPanel, 3);
        Children.Add(logPanel);

        SizeChanged += (_, e) =>
        {
            var w = Math.Max(MinColumnWidth, (e.NewSize.Width - 1) / (Columns().Count + 1));
            if (Math.Abs(w - _width) > 1) { _width = w; _columnsKey = ""; Refresh(); }
        };
        Refresh();
    }

    /// <summary>Original + the meeting's translation languages: during a meeting the ones switched on now; afterwards
    /// the ones the last meeting ended with.</summary>
    private List<string> Columns() =>
        _model.Running || _model.Engine.MeetingTargets.Count == 0 ? _model.Targets.ToList() : _model.Engine.MeetingTargets;

    public void Refresh()
    {
        var chunks = _model.Engine.Chunks;
        _summary.Text = Summary(chunks);
        RefreshLog();
        if (chunks.Count == 0)
        {
            if (_columnsKey != "empty" + _model.Running)
            {
                _columnsKey = "empty" + _model.Running;
                _rowCache.Clear();
                _body.Children.Clear();
                var p = Ui.Column(8,
                    Ui.Icon("", 36, Ui.Tertiary),   // Message
                    Ui.T(_model.Running ? "Listening… what's said appears here with its translations."
                                        : "Press Start meeting. What's said appears here with its translations.", Ui.Body, Ui.Secondary));
                p.HorizontalAlignment = HorizontalAlignment.Center;
                p.VerticalAlignment = VerticalAlignment.Center;
                _body.Children.Add(p);
            }
            return;
        }
        var cols = Columns();
        var behind = _model.Running ? _model.Engine.Status?.Pipeline.BehindS ?? new() : new();
        var key = string.Join(",", cols) + "|" + _width + "|" + string.Join(",", behind.Select(b => $"{b.Key}{Math.Round(b.Value)}"));
        if (key != _columnsKey)
        {
            var columnsChanged = !_columnsKey.StartsWith(string.Join(",", cols) + "|" + _width + "|", StringComparison.Ordinal);
            _columnsKey = key;
            if (columnsChanged) { _rowCache.Clear(); BuildFrame(cols); }
            BuildHeader(cols, behind);
        }
        // Rows: reuse every row whose sentence didn't change; put them in order, newest first.
        var wanted = new List<UIElement>(chunks.Count);
        var seen = new HashSet<int>();
        foreach (var c in chunks)
        {
            seen.Add(c.Seq);
            var k = c.Key;
            if (!_rowCache.TryGetValue(c.Seq, out var cached) || cached.Key != k)
            {
                cached = (k, ChunkRow(c, cols));
                _rowCache[c.Seq] = cached;
            }
            wanted.Add(cached.Row);
        }
        foreach (var s in _rowCache.Keys.Where(s => !seen.Contains(s)).ToList()) _rowCache.Remove(s);
        var same = wanted.Count == _rows.Children.Count;
        for (var i = 0; same && i < wanted.Count; i++) same = ReferenceEquals(wanted[i], _rows.Children[i]);
        if (!same)
        {
            _rows.Children.Clear();
            foreach (var w in wanted) _rows.Children.Add(w);
        }
    }

    private void RefreshLog()
    {
        if (_log.Visibility != Visibility.Visible) return;
        var k = _model.Engine.Log.Count + (_model.Engine.Log.FirstOrDefault()?.Text ?? "");
        if (k == _logKey) return;
        _logKey = k;
        _log.Items.Clear();
        foreach (var l in _model.Engine.Log)
        {
            var t = Ui.T(l.Text, Ui.Caption, l.Level == "error" ? Ui.Red : l.Level is "warn" or "warning" ? Ui.Orange : Ui.Secondary);
            t.FontFamily = new FontFamily("Consolas");
            _log.Items.Add(t);
        }
    }

    private void BuildFrame(List<string> cols)
    {
        _body.Children.Clear();
        var count = cols.Count + 1;
        var inner = new Grid { Width = Math.Max(ActualWidth, _width * count), HorizontalAlignment = HorizontalAlignment.Left };
        inner.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        inner.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        inner.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        _header.Background = Ui.Layer;
        if (_header.Parent is Panel old) old.Children.Remove(_header);
        inner.Children.Add(_header);
        var d = Ui.Divider();
        SetRow(d, 1);
        inner.Children.Add(d);
        if (_rows.Parent is ScrollViewer oldSv) oldSv.Content = null;
        _rows.Children.Clear();
        var vertical = new ScrollViewer { Content = _rows, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollMode = ScrollMode.Disabled };
        SetRow(vertical, 2);
        inner.Children.Add(vertical);
        var horizontal = new ScrollViewer
        {
            Content = inner,
            HorizontalScrollMode = ScrollMode.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
            VerticalScrollMode = ScrollMode.Disabled,
            VerticalScrollBarVisibility = ScrollBarVisibility.Disabled,
        };
        _body.Children.Add(horizontal);
    }

    private Grid NewRowGrid(int count)
    {
        var g = new Grid();
        for (var i = 0; i < count; i++) g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(_width) });
        return g;
    }

    private void BuildHeader(List<string> cols, Dictionary<string, double> behind)
    {
        _header.Children.Clear();
        _header.ColumnDefinitions.Clear();
        for (var i = 0; i <= cols.Count; i++) _header.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(_width) });
        _header.Children.Add(HeaderCell("Original", "as spoken", null));
        for (var i = 0; i < cols.Count; i++)
        {
            var code = cols[i];
            var cell = HeaderCell(OutputLanguage.NameOf(code), OutputLanguage.NativeOf(code),
                                  behind.TryGetValue(code, out var b) ? $"{(int)Math.Round(b)} s behind" : null);
            SetColumn(cell, i + 1);
            _header.Children.Add(cell);
        }
    }

    private static UIElement HeaderCell(string title, string detail, string? lag)
    {
        var g = new Grid { Padding = new Thickness(12, 6, 12, 6) };
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        g.Children.Add(Ui.Row(6, Ui.T(title, Ui.Callout, semibold: true, wrap: false), Ui.T(detail, Ui.Caption, Ui.Secondary, wrap: false)));
        if (lag != null)
        {
            var l = Ui.T(lag, Ui.Caption, Ui.Secondary, wrap: false);
            Ui.Tip(l, "How long after the speaker finished a sentence its translation appeared, for the latest line");
            SetColumn(l, 1);
            g.Children.Add(l);
        }
        return g;
    }

    /// <summary>One sentence across the columns.</summary>
    private UIElement ChunkRow(Chunk chunk, List<string> cols)
    {
        var g = NewRowGrid(cols.Count + 1);
        var original = new StackPanel { Spacing = 4, Padding = new Thickness(12, 8, 12, 8) };
        var meta = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6 };
        if (!string.IsNullOrEmpty(chunk.Src))
        {
            var src = Ui.T(chunk.Src.ToUpperInvariant(), 11, Ui.Secondary, bold: true, wrap: false);
            Ui.Tip(src, $"Spoken in {OutputLanguage.NameOf(chunk.Src)}");
            meta.Children.Add(src);
        }
        if (chunk.WallTime is { } wt)
            meta.Children.Add(Ui.T(DateTimeOffset.FromUnixTimeMilliseconds((long)(wt * 1000)).ToLocalTime().ToString("t"), Ui.Caption, Ui.Tertiary, wrap: false));
        Ui.Tip(meta, HeardDetail(chunk));
        original.Children.Add(meta);
        original.Children.Add(Ui.T(chunk.En.Length == 0 ? "…" : chunk.En, select: true));
        if (chunk.Status is "skipped" or "failed") original.Children.Add(Ui.T(chunk.Note ?? chunk.Status, Ui.Caption, Ui.Secondary));
        g.Children.Add(original);
        for (var i = 0; i < cols.Count; i++)
        {
            var code = cols[i];
            var cell = new StackPanel { Spacing = 4, Padding = new Thickness(12, 8, 12, 8) };
            if (chunk.Langs.TryGetValue(code, out var o))
            {
                var delay = Delay(o);
                if (o.Original || delay != null)
                {
                    var line = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6 };
                    if (o.Original)
                    {
                        var orig = Ui.T("Original", 11, Ui.Secondary, bold: true, wrap: false);
                        Ui.Tip(orig, $"The speaker spoke {OutputLanguage.NameOf(code)}: it goes out as spoken, not translated");
                        line.Children.Add(orig);
                    }
                    if (delay is { } dl)
                    {
                        var dt = Ui.T(dl.Text, Ui.Caption, dl.Color, wrap: false);
                        Ui.Tip(dt, Detail(o));
                        line.Children.Add(dt);
                    }
                    cell.Children.Add(line);
                }
                cell.Children.Add(Ui.T(o.Text.Length == 0 ? (o.Status == "pending" ? "…" : "—") : o.Text,
                                       color: o.Status is "aired" or "captioned" ? Ui.Primary : Ui.Secondary, select: true));
                if (Note(o) is { } note) cell.Children.Add(Ui.T(note, Ui.Caption, Ui.Orange));
            }
            else cell.Children.Add(Ui.T("—", color: Ui.Tertiary));
            SetColumn(cell, i + 1);
            g.Children.Add(cell);
        }
        return new Border { Child = g, BorderBrush = Ui.DividerBrush, BorderThickness = new Thickness(0, 0, 0, 1) };
    }

    private static string HeardDetail(Chunk c)
    {
        var bits = new List<string>();
        if (c.AudioDurS is { } d) bits.Add($"{Format.F(d, "0.0")} s of speech");
        if (c.SttMs is { } ms && ms > 0) bits.Add($"recognised in {(int)ms} ms");
        if (!string.IsNullOrEmpty(c.CutReason)) bits.Add($"cut: {c.CutReason}");
        return string.Join(" · ", bits);
    }

    /// <summary>The line's own delay behind the speaker: caption, then voice. Orange from 8 s, red from 15 s.</summary>
    public static (string Text, Brush Color)? Delay(Chunk.Out o)
    {
        if (o.CaptionLag is not { } cap) return null;
        var text = $"{Format.F(cap, "0.0")} s behind";
        var worst = cap;
        if (o.Status == "aired" && o.AirLag is { } air)
        {
            text += $" · voice {Format.F(air, "0.0")} s";
            worst = Math.Max(worst, air);
        }
        else if (o.Status == "dropped") text += " · no voice";
        return (text, worst > 15 ? Ui.Red : worst > 8 ? Ui.Orange : Ui.Secondary);
    }

    private static string Detail(Chunk.Out o)
    {
        var bits = new List<string> { "Caption: after the speaker finished the sentence" };
        if (o.Engine == "original") bits.Add("passed through as spoken");
        else if (!string.IsNullOrEmpty(o.Engine))
        {
            var who = o.Engine == "claude" ? "the translator" : char.ToUpperInvariant(o.Engine[0]) + o.Engine[1..];
            bits.Add($"translated by {who}" + (o.MtMs is { } mt ? $" in {(int)mt} ms" : ""));
        }
        if (o.TtsMs is { } tts && tts > 0) bits.Add($"voice made in {(int)tts} ms");
        if (o.Speed is { } sp && sp > 1.001) bits.Add($"voice sped up {Format.F(sp, "0.00")}×");
        if (o.Condensed == true) bits.Add("shortened to catch up");
        return string.Join(" · ", bits);
    }

    private static string? Note(Chunk.Out o) => o.Status switch
    {
        "dropped" => "Caption shown; audio skipped to catch up",
        "failed" or "skipped" => string.IsNullOrEmpty(o.Note) ? o.Status : o.Note,
        "merged" => o.Note,
        _ => null,
    };

    private static string Summary(List<Chunk> c)
    {
        if (c.Count == 0) return "";
        var lags = c.SelectMany(x => x.Langs.Values.Select(o => o.CaptionLag)).OfType<double>().OrderBy(x => x).ToList();
        var median = lags.Count == 0 ? "" : $" · captions {Format.F(lags[lags.Count / 2], "0.0")} s after the speaker pauses";
        return $"{c.Count} sentences{median}";
    }
}
