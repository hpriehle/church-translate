import AVFoundation
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

/// The microphone's level before a meeting starts (preflight: "is the sound reaching this Mac?").
@MainActor
final class MicMonitor: ObservableObject {
    @Published private(set) var levelDb: Float = -80
    @Published private(set) var peakDb: Float = -80     // loudest in the last ~10 s
    @Published private(set) var permission = AVCaptureDevice.authorizationStatus(for: .audio)
    private var engine: AVAudioEngine?
    private var history: [Float] = []

    func requestPermission() async {
        if permission == .notDetermined {
            _ = await AVCaptureDevice.requestAccess(for: .audio)
        }
        permission = AVCaptureDevice.authorizationStatus(for: .audio)
    }

    func start() {
        permission = AVCaptureDevice.authorizationStatus(for: .audio)
        guard permission == .authorized, engine == nil else { return }
        let e = AVAudioEngine()
        let input = e.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else { return }
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
        } catch {
            input.removeTap(onBus: 0)
        }
    }

    func stop() {
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        engine = nil
        levelDb = -80
    }

    private func push(_ db: Float) {
        levelDb = db
        history.append(db)
        if history.count > 100 { history.removeFirst(history.count - 100) }
        peakDb = history.max() ?? db
    }
}

/// "Use this Mac's location" for the building pin (Wi-Fi based, so the operator still checks the pin).
final class OneShotLocation: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var continuation: CheckedContinuation<CLLocationCoordinate2D?, Never>?

    func locate() async -> CLLocationCoordinate2D? {
        await withCheckedContinuation { c in
            continuation = c
            manager.delegate = self
            manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
            if manager.authorizationStatus == .notDetermined {
                manager.requestWhenInUseAuthorization()
            }
            manager.requestLocation()
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        continuation?.resume(returning: locations.last?.coordinate)
        continuation = nil
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        continuation?.resume(returning: nil)
        continuation = nil
    }
}
