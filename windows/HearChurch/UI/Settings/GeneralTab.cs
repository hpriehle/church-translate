using HearChurch.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace HearChurch.UI.Settings;

/// <summary>This PC and help. The key is in the AI tab (own-key congregations only); languages are in Languages.</summary>
public sealed class GeneralTab : SettingsTab
{
    public GeneralTab(AppModel model) : base(model, "General")
    {
        var atLogin = new ToggleSwitch { Header = "Open Hear Church when this PC starts", IsOn = LoginItem.Enabled };
        atLogin.Toggled += (_, _) => LoginItem.Set(atLogin.IsOn);
        Add(Ui.Group("This PC", null, atLogin));

        var share = new ToggleSwitch
        {
            IsOn = model.Profile.ShareLogs,
            Header = Ui.Column(2, Ui.T("Share diagnostic logs with Hear Church support"), Ui.T(DiagnosticLogText.Detail, Ui.Callout, Ui.Secondary)),
        };
        share.Toggled += (_, _) => model.SetShareLogs(share.IsOn);
        var send = Ui.Button("Send logs to support…", () => _ = SendLogs());
        send.IsEnabled = model.Profile.SetupDone;
        Ui.Tip(send, "Send the last 7 days of diagnostic logs now, with a note on what went wrong");
        var links = SignInStep.HelpLinks();
        UIElement? dev = Updates.Channel == "dev"
            ? Ui.Label("Dev updates on", "", Ui.Secondary)
            : null;
        if (dev != null) Ui.Tip(dev, "This PC also gets test builds (the dev update channel).");
        Add(Ui.Group("About and help", null, share, send, Ui.Button("Show the engine's log", MainView.ShowLog), links, dev));
    }

    /// <summary>"Send logs to support": the last 7 days of diagnostic logs, with an optional note, once.</summary>
    private async Task SendLogs()
    {
        var note = new TextBox { PlaceholderText = "What went wrong? (optional)", AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 90, MaxLength = 1000 };
        var error = Ui.T("", Ui.Callout, Ui.Red);
        var body = Ui.Column(12, Ui.T("Sends the last 7 days of diagnostic logs from this PC. " + DiagnosticLogText.Detail, Ui.Callout, Ui.Secondary), note, error);
        body.Width = 420;
        var dlg = new ContentDialog
        {
            Title = "Send logs to support",
            Content = body,
            PrimaryButtonText = "Send",
            CloseButtonText = "Cancel",
            DefaultButton = ContentDialogButton.Primary,
            XamlRoot = XamlRoot,
        };
        int? reference = null;
        var busy = false;
        dlg.PrimaryButtonClick += async (_, e) =>
        {
            if (reference != null) return;   // "Done"
            e.Cancel = true;
            if (busy) return;
            busy = true;
            dlg.PrimaryButtonText = "Sending…";
            dlg.IsPrimaryButtonEnabled = false;
            try
            {
                reference = await Model.SendLogsToSupport(note.Text.Trim());
                body.Children.Clear();
                body.Children.Add(Ui.Row(6, Ui.Icon("", 14, Ui.Green), Ui.T($"Sent. Reference #{reference}", Ui.Body, Ui.Green, select: true)));
                body.Children.Add(Ui.T("Give this number to Hear Church support so they can find your logs.", Ui.Callout, Ui.Secondary));
                dlg.PrimaryButtonText = "Done";
                dlg.CloseButtonText = "";
            }
            catch (Exception x)
            {
                error.Text = x.Message;
                dlg.PrimaryButtonText = "Send";
            }
            dlg.IsPrimaryButtonEnabled = true;
            busy = false;
        };
        try { await dlg.ShowAsync(); } catch { }
    }
}
