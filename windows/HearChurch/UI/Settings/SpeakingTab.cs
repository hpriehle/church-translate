using System.Text.Json.Nodes;
using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace HearChurch.UI.Settings;

/// <summary>How each of the meeting's translation languages is read aloud. The engine has a speed per language
/// (app/config.py: French in [tts].speed, the others in [voices.&lt;code&gt;].speed; 1 = the voice's own pace), and one
/// volume for all.</summary>
public sealed class SpeakingTab : SettingsTab
{
    public SpeakingTab(AppModel model) : base(model, "Speaking")
    {
        Add(() => $"{Model.Engine.Settings != null}|{Model.Running}|{string.Join(",", Model.Targets)}", Build);
    }

    private UIElement Build()
    {
        if (Model.Engine.Settings is not { } s) return Ui.T("The translation engine is starting…", Ui.Body, Ui.Secondary);
        var p = new StackPanel { Spacing = 22 };
        var test = Ui.Button("Play a test sentence", () => _ = Model.Engine.Post("api/test-voice"));
        test.IsEnabled = Model.Running;
        Ui.Tip(test, "Listening phones hear a short sentence. Start the meeting first.");
        p.Children.Add(Ui.Group("Test", null, test));

        p.Children.Add(Ui.Group("Speed",
            "How fast each translation language of the meeting is read aloud. 100% is the voice's own pace. Voices also speed up by themselves when they fall behind the speaker.",
            Model.Targets.Select(code => (UIElement)SettingSlider(OutputLanguage.NameOf(code), Speed(code, s.Settings), 0.7, 1.6, 0.05, v =>
            {
                v = Math.Round(v * 100) / 100;
                _ = Model.Engine.Set(code == "fr"
                    ? new JsonObject { ["tts"] = new JsonObject { ["speed"] = v } }
                    : new JsonObject { ["voices"] = new JsonObject { [code] = new JsonObject { ["speed"] = v } } });
            })).ToArray()));

        p.Children.Add(Ui.Group("Volume", null,
            SettingSlider("Every language", s.Settings.Tts.Volume, 0.1, 1.0, 0.05, v =>
                _ = Model.Engine.Set(new JsonObject { ["tts"] = new JsonObject { ["volume"] = Math.Round(v * 100) / 100 } }))));
        return p;
    }

    private static double Speed(string code, EngineSettings.SettingsInfo s) =>
        (code == "fr" ? s.Tts.Speed : s.Voices.GetValueOrDefault(code)?.Speed) ?? 1.0;

    /// <summary>A slider with its value as a percentage; the engine hears the value once the slider rests.</summary>
    private static UIElement SettingSlider(string title, double value, double min, double max, double step, Action<double> commit)
    {
        var label = Ui.T(Percent(value), Ui.Body, wrap: false);
        label.Width = 52;
        label.TextAlignment = TextAlignment.Right;
        var slider = new Slider { Minimum = min, Maximum = max, StepFrequency = step, Value = Math.Clamp(value, min, max), Width = 240, IsThumbToolTipEnabled = false };
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(slider, title);
        var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(450) };
        timer.Tick += (_, _) => { timer.Stop(); commit(slider.Value); };
        slider.ValueChanged += (_, e) =>
        {
            label.Text = Percent(e.NewValue);
            timer.Stop();
            timer.Start();
        };
        return Ui.Field(Ui.T(title), Ui.Row(8, slider, label));
    }

    private static string Percent(double v) => $"{(int)Math.Round(v * 100)}%";
}
