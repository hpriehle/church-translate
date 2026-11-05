import Combine
import Foundation
import Sparkle

/// Sparkle updates from hearchurch.com/appcast.xml (settings in project.yml). Updates download in the
/// background and install when the app quits. No check runs while a meeting is being translated, so
/// nothing pops up or relaunches during a service.
@MainActor
final class Updater: NSObject, ObservableObject, SPUUpdaterDelegate {
    static let shared = Updater()

    @Published private(set) var canCheck = false
    private var controller: SPUStandardUpdaterController!

    private override init() {
        super.init()
        controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: self, userDriverDelegate: nil)
        controller.updater.publisher(for: \.canCheckForUpdates).assign(to: &$canCheck)
    }

    func checkForUpdates() { controller.checkForUpdates(nil) }

    nonisolated func updater(_ updater: SPUUpdater, mayPerform updateCheck: SPUUpdateCheck) throws {
        let running = MainActor.assumeIsolated { AppModel.shared.running }
        if running && updateCheck != .updates {   // a check the operator asked for still runs
            throw NSError(domain: "HearChurch", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "A meeting is being translated."])
        }
    }
}
