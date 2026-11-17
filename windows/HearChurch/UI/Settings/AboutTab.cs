using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace HearChurch.UI.Settings;

/// <summary>The Mac's About panel and "About and help" links: the version and updates, the voices' credits (the CC BY
/// attributions their licences ask for, as on hearchurch.com/credits), Wolof speech recognition's, and the
/// open-source notices.</summary>
public sealed class AboutTab : SettingsTab
{
    private const string HiFiTts = "fine-tuned from a model trained on the Hi-Fi TTS dataset (reader 92, Cori Samuel), by Evelina Bakhturina, Vitaly Lavrukhin, Boris Ginsburg and Yang Zhang (NVIDIA)";

    /// <summary>The voices on Windows (download/src/credits.html).</summary>
    private static readonly (string Language, string Voice, string Source, string Licence)[] Voices =
    {
        ("French", "Male 1 - Julien", "French voice “tom” by Tjiho", "AGPLv3"),
        ("Wolof", "Male 1 - Moussa", "SpeechT5 Wolof, by Bilal Faye, with Microsoft's SpeechT5 vocoder", "MIT"),
        ("Wolof", "Female 1 - Fatou", "SpeechT5 Wolof, by Bilal Faye, with Microsoft's SpeechT5 vocoder", "MIT"),
        ("French", "Female 1 - Camille", "The SIWIS French Speech Synthesis Database, by Junichi Yamagishi, Pierre-Edouard Honnet, Philip Garner and Alexandros Lazaridis, " + HiFiTts, "CC BY 4.0; Hi-Fi TTS: CC BY 4.0"),
        ("Spanish", "Male 1 - Carlos", "Ald Mexican Spanish speech dataset", "Unlicense"),
        ("Spanish", "Female 1 - Lucía", "Piper TTS Spanish, by HirCoir", "Apache 2.0"),
        ("Russian", "Male 1 - Ivan", "Open Home Foundation voice datasets (“denis”)", "CC0"),
        ("Russian", "Male 2 - Alexei", "Open Home Foundation voice datasets (“dmitri”), " + HiFiTts, "CC0; Hi-Fi TTS: CC BY 4.0"),
        ("Portuguese", "Male 1 - João", "Open Home Foundation voice datasets (“cadu”)", "CC0"),
        ("Portuguese", "Male 2 - Pedro", "Open Home Foundation voice datasets (“faber”), " + HiFiTts, "CC0; Hi-Fi TTS: CC BY 4.0"),
        ("Ukrainian", "Female 1 - Olena", "Ukrainian TTS datasets (“tetiana”), voice by Roman Stasyshyn", "Apache 2.0"),
        ("Ukrainian", "Male 1 - Andriy", "Ukrainian TTS datasets (“oleksa”), voice by Roman Stasyshyn", "Apache 2.0"),
        ("Persian", "Male 1 - Ali", "Datacula TTS databases (“amir”)", "CC0"),
        ("Persian", "Male 2 - Reza", "Datacula TTS databases (“ganji”)", "CC0"),
        ("Nepali", "Male 1 - Ram", "Open Home Foundation voice datasets (“chitwan”)", "CC0"),
        ("Vietnamese", "Female 1 - Lan", "VAIS-1000: A Vietnamese Speech Synthesis Corpus, submitted by Truong Do", "CC BY 4.0"),
        ("Chinese", "Female 1 - Mei", "Open Home Foundation voice datasets (“chaowen”), " + HiFiTts, "CC0; Hi-Fi TTS: CC BY 4.0"),
        ("English", "Male 1 - James", "Open Home Foundation voice datasets (“mike”)", "CC0"),
        ("English", "Female 1 - Mary", "Open Home Foundation voice datasets (“kathleen”), " + HiFiTts, "CC0; Hi-Fi TTS: CC BY 4.0"),
    };

    public AboutTab(AppModel model) : base(model, "About")
    {
        var version = Ui.T(VersionLine(), Ui.Body, select: true);
        Button? check = null;
        check = Ui.Button("Check for Updates", async () =>
        {
            check!.IsEnabled = false;
            version.Text = "Checking for updates…";
            var answer = await App.Updates.Check();
            version.Text = VersionLine() + "\n" + answer;
            check.IsEnabled = true;
        });
        Add(Ui.Group("Hear Church", null, version, check));

        var links = SignInStep.HelpLinks();
        Add(Ui.Group("Help", null, links));

        Add(Ui.Group("Voice credits",
            "The names shown in the app are our own labels and are not the names of the people recorded. Each voice is used unchanged, apart from adjusting its speed and volume. Except for Wolof, these voices come from the Piper voices collection, and speech is generated with Piper and eSpeak NG (GPL-3.0).",
            new UIElement[] { Ui.T("Hear Church speaks with voices other people recorded and shared. Thank you to them.", Ui.Callout, Ui.Secondary) }
                .Concat(Voices.Select(v => (UIElement)Ui.Column(2,
                    Ui.T($"{v.Language} · {v.Voice}", Ui.Callout, semibold: true),
                    Ui.T($"{v.Source}. Licence: {v.Licence}.", Ui.Caption, Ui.Secondary, select: true))))
                .Append(Ui.Link("Licences and sources: hearchurch.com/credits", "https://hearchurch.com/credits"))
                .ToArray()));

        Add(Ui.Group("Wolof speech recognition", null,
            Ui.T("When someone speaks Wolof, Hear Church understands it with Omnilingual ASR, by Meta (Apache 2.0). Thank you to them.", Ui.Callout, Ui.Secondary)));

        var notices = Ui.Button("Open-source notices", SignInStep.ShowNotices);
        Ui.Tip(notices, "The open-source parts of the app, their licences, and where to get their source code");
        Add(Ui.Group("Open-source software",
            null,
            Ui.T("The app includes open-source software under the GNU GPL and LGPL, among others: Piper and eSpeak NG (GPL v3), LAME and libsoxr (LGPL), and FFmpeg (LGPL). Their licences and notices are in the app (Open-source notices). Their complete source code, for every version of the app, is at hearchurch.com/source; for at least three years after we last distribute a version, you may also ask support@hearchurch.com for it on a physical medium, for no more than our cost of sending it.", Ui.Callout, Ui.Secondary),
            Ui.Row(12, notices, Ui.Link("hearchurch.com/source", "https://hearchurch.com/source"))));
    }

    /// <summary>"Hear Church 0.4.0 (12) for Windows (test build)", plus the update channel when it isn't public.</summary>
    public static string VersionLine()
    {
        var u = App.Updates;
        var channel = u.Installed && Updates.Channel != "public" ? $", {Updates.Channel} updates" : "";
        var line = $"Hear Church {u.Version} for Windows (test build{channel})";
        return u.Ready != null ? $"{line}\nHear Church {u.Ready} is ready. It is installed the next time Hear Church quits." : line;
    }
}
