import AppKit
import AVFoundation
import CoreAudio
import CoreLocation
import IOKit.ps
import IOKit.pwr_mgt
import Network
import ServiceManagement

/// Keeps the Mac (and its screen) awake while a meeting runs. Closing the lid still sleeps it, which is why the
/// window says to keep the lid open and the Mac plugged in.
final class KeepAwake {
    private var ids: [IOPMAssertionID] = []

    var active: Bool { !ids.isEmpty }

    func set(_ on: Bool) {
        if on == active { return }
        if on {
            for type in [kIOPMAssertionTypePreventUserIdleSystemSleep, kIOPMAssertionTypePreventUserIdleDisplaySleep] {
                var id = IOPMAssertionID(0)
                if IOPMAssertionCreateWithName(type as CFString, IOPMAssertionLevel(kIOPMAssertionLevelOn),
                                               "Hear Church is translating a meeting" as CFString, &id) == kIOReturnSuccess {
                    ids.append(id)
                }
            }
        } else {
            ids.forEach { IOPMAssertionRelease($0) }
            ids = []
        }
    }
}

enum Power {
    /// True when running on the battery (a MacBook unplugged).
    static var onBattery: Bool {
        guard let info = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let type = IOPSGetProvidingPowerSourceType(info)?.takeUnretainedValue() as String? else { return false }
        return type == kIOPSBatteryPowerValue
    }
}

enum LoginItem {
    static var enabled: Bool { SMAppService.mainApp.status == .enabled }

    static func set(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            NSLog("launch at login: \(error)")
        }
    }
}

/// Internet up or down, as the system sees it.
final class NetworkWatch: ObservableObject {
    @Published private(set) var online = true
    private let monitor = NWPathMonitor()

    init() {
        monitor.pathUpdateHandler = { [weak self] path in
            DispatchQueue.main.async { self?.online = path.status == .satisfied }
        }
        monitor.start(queue: DispatchQueue(label: "network-watch"))
    }
}

/// The level alone, ~10 times a second. Only the meters watch it, so the rest of the window (and an open
/// menu, such as the input picker) doesn't redraw with every buffer.
@MainActor
final class MicLevel: ObservableObject {
    @Published var db: Float = -80
}

/// The microphone before a meeting starts (preflight: "is the sound reaching this Mac?"), from the input the
/// operator chose. During a meeting the engine has the microphone instead.
@MainActor
final class MicMonitor: ObservableObject {
    @Published private(set) var permission = AVCaptureDevice.authorizationStatus(for: .audio)
    @Published private(set) var heardSound = false    // something louder than -50 dB in the last ~10 s
    let level = MicLevel()
    /// A microphone was plugged in or taken away.
    var onDevicesChanged: (() -> Void)?
    private var engine: AVAudioEngine?
    private var device = ""
    private var history: [Float] = []
    private var observers: [NSObjectProtocol] = []

    init() {
        var addr = Self.address(kAudioHardwarePropertyDevices)
        AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &addr, .main) { [weak self] _, _ in
            MainActor.assumeIsolated { self?.devicesChanged() }
        }
    }

    static func openPrivacySettings() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!)
    }

    func requestPermission() async {
        if permission == .notDetermined {
            _ = await AVCaptureDevice.requestAccess(for: .audio)
        }
        permission = AVCaptureDevice.authorizationStatus(for: .audio)
    }

    /// Listen to `device` (its name as the engine lists it; "" = the Mac's default input).
    func start(device name: String? = nil) {
        if let name, name != device {
            device = name
            stop()
        }
        permission = AVCaptureDevice.authorizationStatus(for: .audio)
        guard permission == .authorized, engine == nil else { return }
        let e = AVAudioEngine()
        let input = e.inputNode
        if !device.isEmpty, var id = Self.deviceID(named: device), let unit = input.audioUnit {
            AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &id,
                                 UInt32(MemoryLayout<AudioDeviceID>.size))
        }
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { return }
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buf, _ in
            guard let ch = buf.floatChannelData?[0] else { return }
            let n = Int(buf.frameLength)
            var sum: Float = 0
            for i in 0..<n { sum += ch[i] * ch[i] }
            let db = 10 * log10(max(sum / Float(max(n, 1)), 1e-10))
            Task { @MainActor in self?.push(db) }
        }
        do {
            try e.start()
            engine = e
            // The input changed under us (headphones unplugged, another default): listen again.
            observers.append(NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: e,
                                                                    queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.restart() }
            })
        } catch {
            input.removeTap(onBus: 0)
        }
    }

    func stop() {
        observers.forEach(NotificationCenter.default.removeObserver)
        observers = []
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        engine = nil
        level.db = -80
        history = []
    }

    private var listening: Bool { engine != nil }

    private func restart() {
        guard listening else { return }
        stop()
        start()
    }

    private func devicesChanged() {
        restart()
        onDevicesChanged?()
    }

    private func push(_ db: Float) {
        level.db = db
        history.append(db)
        if history.count > 100 { history.removeFirst(history.count - 100) }
        let heard = (history.max() ?? db) > -50
        if heard != heardSound { heardSound = heard }
    }

    // MARK: Core Audio

    private static func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                   mElement: kAudioObjectPropertyElementMain)
    }

    /// The Core Audio device with this name (the names PortAudio, and so the engine, show).
    static func deviceID(named name: String) -> AudioDeviceID? {
        var addr = address(kAudioHardwarePropertyDevices)
        let system = AudioObjectID(kAudioObjectSystemObject)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size) == noErr else { return nil }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids) == noErr else { return nil }
        for id in ids {
            var nameAddr = address(kAudioObjectPropertyName)
            var cf: Unmanaged<CFString>?
            var s = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
            if AudioObjectGetPropertyData(id, &nameAddr, 0, nil, &s, &cf) == noErr,
               let n = cf?.takeRetainedValue() as String?, n == name { return id }
        }
        return nil
    }
}

/// "Use this Mac's location" for the building pin (Wi-Fi based, so the operator still checks the pin).
final class OneShotLocation: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var continuation: CheckedContinuation<CLLocationCoordinate2D?, Never>?
    private var requested = false

    /// Waits for the permission answer before asking for the location; gives up after 15 s.
    func locate() async -> CLLocationCoordinate2D? {
        await withCheckedContinuation { c in
            continuation = c
            manager.delegate = self
            manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
            DispatchQueue.main.asyncAfter(deadline: .now() + 15) { [weak self] in self?.finish(nil) }
            switch manager.authorizationStatus {
            case .notDetermined: manager.requestWhenInUseAuthorization()   // continues in locationManagerDidChangeAuthorization
            case .denied, .restricted: finish(nil)
            default: request()
            }
        }
    }

    private func request() {
        guard !requested else { return }
        requested = true
        manager.requestLocation()
    }

    private func finish(_ c: CLLocationCoordinate2D?) {
        continuation?.resume(returning: c)
        continuation = nil
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        switch manager.authorizationStatus {
        case .notDetermined: break
        case .denied, .restricted: finish(nil)
        default: request()
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        finish(locations.last?.coordinate)
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        finish(nil)
    }
}
