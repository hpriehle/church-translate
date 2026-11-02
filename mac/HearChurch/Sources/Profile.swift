import Foundation

/// What the operator entered during setup, plus what hearchurch.com handed back. Secrets (API key, device
/// token) are not in here: they live in the Keychain.
struct MeetingTime: Codable, Hashable, Identifiable {
    var id = UUID()
    var day: String = "sun"      // mon … sun
    var time: String = "10:00"   // 24-hour HH:MM

    enum CodingKeys: String, CodingKey { case day, time }
}

struct Profile: Codable {
    var church: String = ""
    var unitType: String = "ward"           // ward | branch | congregation | parish | other
    var name: String = ""
    var stake: String = ""
    var address: String = ""
    var lat: Double?
    var lng: Double?
    var meetingTimes: [MeetingTime] = [MeetingTime()]
    var timezone: String = TimeZone.current.identifier
    var languages: [String] = ["fr"]
    var email: String = ""
    var approved = false
    var slug: String = ""
    var claim: String = ""                  // while waiting for the e-mail link to be clicked
    var setupDone = false

    static var url: URL { AppPaths.home.appendingPathComponent("profile.json") }

    static func load() -> Profile {
        guard let data = try? Data(contentsOf: url),
              let p = try? JSONDecoder().decode(Profile.self, from: data) else { return Profile() }
        return p
    }

    func save() {
        try? FileManager.default.createDirectory(at: AppPaths.home, withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(self) { try? data.write(to: Profile.url, options: .atomic) }
    }

    var isLatterDaySaint: Bool { church == Churches.latterDaySaints }
    var unitLabel: String {
        switch unitType {
        case "ward": return "Ward"
        case "branch": return "Branch"
        case "parish": return "Parish"
        default: return "Congregation"
        }
    }
}

enum Churches {
    static let latterDaySaints = "The Church of Jesus Christ of Latter-day Saints"
    static let all = [latterDaySaints, "Catholic", "Baptist", "Methodist", "Non-denominational"]
}

/// Output languages offered at launch. Spanish and Russian arrive with Phase 4 (voices and prompts).
struct OutputLanguage: Identifiable {
    let id: String
    let name: String
    let available: Bool
    static let all = [
        OutputLanguage(id: "fr", name: "French", available: true),
        OutputLanguage(id: "wo", name: "Wolof", available: true),
        OutputLanguage(id: "es", name: "Spanish", available: false),
        OutputLanguage(id: "ru", name: "Russian", available: false),
    ]
}

enum AppPaths {
    /// ~/Library/Application Support/Hear Church: settings, logs, glossaries, downloaded voices.
    static var home: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("Hear Church", isDirectory: true)
    }
    static var logs: URL { home.appendingPathComponent("logs", isDirectory: true) }

    /// hearchurch.com, or another hub for testing: `defaults write com.hearchurch.HearChurch HubURL http://127.0.0.1:8000`
    static var hub: URL {
        let s = UserDefaults.standard.string(forKey: "HubURL") ?? "https://hearchurch.com"
        return URL(string: s.trimmingCharacters(in: CharacterSet(charactersIn: "/"))) ?? URL(string: "https://hearchurch.com")!
    }
}
