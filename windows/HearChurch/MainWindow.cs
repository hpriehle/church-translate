using HearChurch.Core;
using HearChurch.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace HearChurch;

/// <summary>The one window: setup until it's done, then the operator page (the Mac's RootView).</summary>
public sealed class MainWindow : Window
{
    private readonly AppModel _model;
    private readonly Grid _root = new();
    public MainView? MainView { get; private set; }
    public OnboardingView? Onboarding { get; private set; }
    public Grid Root => _root;

    public MainWindow(AppModel model)
    {
        _model = model;
        Title = "Hear Church";
        AppWindow.Resize(new Windows.Graphics.SizeInt32(1180, 800));
        AppWindow.SetIcon(Path.Combine(AppContext.BaseDirectory, "Assets", "HearChurch.ico"));
        _root.Background = Ui.WindowBackground;
        Content = _root;
        model.SetupFinished += () => Show();
        model.SignedOutAction += () => Show();
        model.Changed += Refresh;
        VisibilityChanged += (_, e) => model.WindowVisible = e.Visible;
        Show();
    }

    private void Show()
    {
        MainView?.Detach();
        _root.Children.Clear();
        MainView = null;
        Onboarding = null;
        if (_model.Profile.SetupDone)
        {
            MainView = new MainView(_model);
            _root.Children.Add(MainView);
        }
        else
        {
            Onboarding = new OnboardingView(_model);
            _root.Children.Add(Onboarding);
        }
    }

    private void Refresh()
    {
        if ((MainView != null) != _model.Profile.SetupDone) Show();
        MainView?.Refresh();
        Onboarding?.Refresh();
    }

    public void Tick() => MainView?.Tick();
}
