<#
The Windows installer on the CI runner (W3, docs/windows-release.md), as an office PC would see it:

 1. silent per-user install from Setup.exe: app files, Start menu and desktop shortcuts, the entry in Settings > Apps;
 2. the app starts and its engine (the installed one) answers on /api/status;
 3. update: the feed ("UpdateURL" in app-settings.json) is a local folder holding a newer test version; the app finds
    it at launch, downloads it in the background, and installs it when it quits (HearChurch.exe --quit); the new
    version then starts and its engine answers;
 4. uninstall (the QuietUninstallString Settings > Apps runs): no app files, shortcuts, Apps entry or start-at-login
    entry left; the data folder %LOCALAPPDATA%\HearChurch stays.

Writes install.json (every step) and the app's logs to -Out.

    pwsh scripts/windows_install_test.ps1 -Setup releases\HearChurchApp-dev-Setup.exe -Feed testfeed `
        -V1 0.1.0-dev.12 -V2 0.1.1-test.12 -Out install-test
#>
param(
    [Parameter(Mandatory)] [string] $Setup,
    [Parameter(Mandatory)] [string] $Feed,
    [Parameter(Mandatory)] [string] $V1,
    [Parameter(Mandatory)] [string] $V2,
    [string] $Out = 'install-test'
)
$ErrorActionPreference = 'Stop'
$PackId = 'HearChurchApp'
$Root = Join-Path $env:LOCALAPPDATA $PackId
$Data = Join-Path $env:LOCALAPPDATA 'HearChurch'
$Exe = Join-Path $Root 'current\HearChurch.exe'
$AppsKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$PackId"
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$Desktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Hear Church.lnk'
$StartMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Hear Church.lnk'
New-Item -ItemType Directory -Force $Out | Out-Null
$Out = (Resolve-Path $Out).Path
$results = [ordered]@{}
$t0 = Get-Date

function Step([string] $name, $value) {
    $results[$name] = $value
    Write-Host ("[{0,5:n0} s] {1}: {2}" -f ((Get-Date) - $t0).TotalSeconds, $name, ($value | ConvertTo-Json -Compress -Depth 4))
}

function WaitFor([scriptblock] $cond, [int] $seconds, [string] $what) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        $r = & $cond
        if ($r) { return $r }
        Start-Sleep -Seconds 2
    }
    throw "timed out after $seconds s: $what"
}

function AppProcesses { @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Root, 'OrdinalIgnoreCase') }) }

function InstalledVersion {
    $f = Join-Path $Root 'current\sq.version'
    if (Test-Path $f) { return ([xml](Get-Content $f -Raw)).package.metadata.version }
    return $null
}

function UpdatesLog {
    $f = Join-Path $Data 'logs\updates.log'
    if (Test-Path $f) { return Get-Content $f -Raw }
    return ''
}

# The installed engine answers: the pid the app saved, that process is the exe under the install folder, and one of
# its listening ports serves /api/status.
function EngineAnswers {
    $pidFile = Join-Path $Data 'engine.pid'
    if (-not (Test-Path $pidFile)) { return $null }
    $enginePid = 0
    if (-not [int]::TryParse((Get-Content $pidFile -Raw).Trim(), [ref] $enginePid)) { return $null }
    $p = Get-Process -Id $enginePid -ErrorAction SilentlyContinue
    if (-not $p -or -not $p.Path.StartsWith($Root, 'OrdinalIgnoreCase')) { return $null }
    foreach ($c in @(Get-NetTCPConnection -OwningProcess $enginePid -State Listen -ErrorAction SilentlyContinue)) {
        try {
            $st = Invoke-RestMethod "http://127.0.0.1:$($c.LocalPort)/api/status" -TimeoutSec 5
            if ($st) { return @{ pid = $enginePid; port = $c.LocalPort; exe = $p.Path } }
        } catch { }
    }
    return $null
}

function Quit([string] $what) {
    Start-Process -FilePath $Exe -ArgumentList '--quit' -Wait
    WaitFor { (AppProcesses).Count -eq 0 } 90 "$what quits (app and engine)" | Out-Null
}

function SaveLogs {
    $logs = Join-Path $Out 'logs'
    New-Item -ItemType Directory -Force $logs | Out-Null
    if (Test-Path (Join-Path $Data 'logs')) { Copy-Item (Join-Path $Data 'logs\*') $logs -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path $Root) {
        Get-ChildItem $Root -Filter '*.log' -Recurse -ErrorAction SilentlyContinue |
            ForEach-Object { Copy-Item $_.FullName (Join-Path $logs "velopack-$($_.Name)") -Force }
    }
}

$failed = $null
try {
    # A fresh data folder whose app settings point the update check at the local test feed.
    if (Test-Path $Data) { Remove-Item $Data -Recurse -Force }
    New-Item -ItemType Directory -Force $Data | Out-Null
    $feedPath = (Resolve-Path $Feed).Path
    @{ UpdateURL = $feedPath } | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $Data 'app-settings.json')
    if (Test-Path $Root) { throw "$Root exists before the install" }

    # 1. Install, silently and without admin rights.
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process -FilePath (Resolve-Path $Setup).Path -ArgumentList '--silent' -PassThru
    if (-not $p.WaitForExit(600000)) { throw 'Setup.exe still running after 10 minutes' }
    if ($p.ExitCode -ne 0) { throw "Setup.exe exit code $($p.ExitCode)" }
    Step 'install' @{ seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1); setup = (Get-Item $Setup).Length }
    foreach ($f in @($Exe, (Join-Path $Root 'current\engine\hearchurch-engine.exe'), (Join-Path $Root 'Update.exe'))) {
        if (-not (Test-Path $f)) { throw "missing after install: $f" }
    }
    $apps = Get-ItemProperty $AppsKey -ErrorAction SilentlyContinue
    if (-not $apps) { throw "no entry in Settings > Apps ($AppsKey)" }
    Step 'apps-entry' @{ name = $apps.DisplayName; version = $apps.DisplayVersion; publisher = $apps.Publisher; quiet = $apps.QuietUninstallString }
    if ($apps.DisplayName -ne 'Hear Church') { throw "Apps entry is named '$($apps.DisplayName)'" }
    $missing = @($Desktop, $StartMenu | Where-Object { -not (Test-Path $_) })
    if ($missing) {
        $found = Get-ChildItem ([Environment]::GetFolderPath('Desktop')), ([Environment]::GetFolderPath('Programs')) -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue | ForEach-Object FullName
        throw "missing shortcuts: $($missing -join ', '); found: $($found -join ', ')"
    }
    Step 'shortcuts' @($Desktop, $StartMenu)
    $v = InstalledVersion
    if ($v -ne $V1) { throw "installed version $v, expected $V1" }
    Step 'installed-version' $v

    # 2. The app starts (Setup starts it; if not, the shortcut would) and its engine answers.
    Start-Sleep -Seconds 15
    if ((AppProcesses | Where-Object Name -eq 'HearChurch').Count -eq 0) {
        Step 'started-by-setup' $false
        Start-Process -FilePath $Exe
    } else { Step 'started-by-setup' $true }
    WaitFor { (AppProcesses | Where-Object Name -eq 'HearChurch').Count -gt 0 } 60 'HearChurch.exe running' | Out-Null
    Step 'engine-v1' (WaitFor { EngineAnswers } 300 'the engine answers (v1)')

    # 3. The update: found at launch, downloaded in the background, installed on quit.
    WaitFor { (UpdatesLog) -match [regex]::Escape("downloaded $V2") } 600 "the app downloads $V2" | Out-Null
    Step 'downloaded' (Get-ChildItem (Join-Path $Root 'packages') -ErrorAction SilentlyContinue | ForEach-Object Name)
    if ((InstalledVersion) -ne $V1) { throw 'the update was applied before quitting' }
    Quit 'v1'
    Step 'applied' (WaitFor { $iv = InstalledVersion; if ($iv -eq $V2) { $iv } } 300 "$V2 installed after quitting")
    WaitFor { -not (Get-Process -Name Update -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Root, 'OrdinalIgnoreCase') }) } 120 'the updater finishes' | Out-Null
    Step 'apps-version' (Get-ItemProperty $AppsKey).DisplayVersion

    Start-Process -FilePath $Exe
    Step 'engine-v2' (WaitFor { EngineAnswers } 300 'the engine answers (v2)')
    WaitFor { (UpdatesLog) -match [regex]::Escape("installed $V2") } 60 "the app reports $V2" | Out-Null
    Step 'app-reports' $V2
    Quit 'v2'

    # 4. Uninstall as Settings > Apps does. Start at login on first, so the uninstall hook must take it off.
    Set-ItemProperty $RunKey -Name 'HearChurch' -Value "`"$Exe`" --background"
    SaveLogs
    $quiet = (Get-ItemProperty $AppsKey).QuietUninstallString
    if ($quiet -notmatch '^"([^"]+)"\s*(.*)$') { throw "unexpected QuietUninstallString: $quiet" }
    $u = Start-Process -FilePath $Matches[1] -ArgumentList $Matches[2] -PassThru
    if (-not $u.WaitForExit(300000)) { throw 'uninstall still running after 5 minutes' }
    Step 'uninstall-exit' $u.ExitCode
    WaitFor { -not (Test-Path $Exe) -and -not (Test-Path $AppsKey) } 120 'app files and Apps entry removed' | Out-Null
    # Update.exe deletes itself (and the folder) just after it exits.
    $left = $null
    try {
        WaitFor { -not (Test-Path $Root) -or -not (Get-ChildItem $Root -Recurse -File -ErrorAction SilentlyContinue) } 60 'install folder empty' | Out-Null
    } catch { $left = @(Get-ChildItem $Root -Recurse -File | ForEach-Object FullName) }
    if ($left) { throw "files left in ${Root}: $($left -join ', ')" }
    $shortcuts = @($Desktop, $StartMenu | Where-Object { Test-Path $_ })
    if ($shortcuts) { throw "shortcuts left: $($shortcuts -join ', ')" }
    if ((Get-ItemProperty $RunKey -ErrorAction SilentlyContinue).HearChurch) { throw 'start-at-login entry left in HKCU Run' }
    if ((AppProcesses).Count -gt 0) { throw 'app processes still running' }
    Step 'uninstalled' @{ folder = (Test-Path $Root); appsEntry = (Test-Path $AppsKey); runKey = $false }
    $kept = @(Get-ChildItem $Data -ErrorAction SilentlyContinue | ForEach-Object Name)
    if (-not (Test-Path (Join-Path $Data 'app-settings.json'))) { throw "the data folder wasn't kept: $Data" }
    Step 'data-kept' $kept
} catch {
    $failed = "$_"
    Write-Host "FAILED: $failed"
    SaveLogs
} finally {
    $results['seconds'] = [math]::Round(((Get-Date) - $t0).TotalSeconds)
    $results['ok'] = -not $failed
    if ($failed) { $results['error'] = $failed }
    $results | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $Out 'install.json')
}
if ($failed) { exit 1 }
