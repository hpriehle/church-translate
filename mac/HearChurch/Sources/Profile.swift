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
    var languagesPicked = false             // the operator chose languages, so their downloads may start
    var setupDone = false

    init() {}

    /// Every field is optional in the file, so profiles saved by older versions still load.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func v<T: Decodable>(_ k: CodingKeys, _ d: T) -> T { ((try? c.decodeIfPresent(T.self, forKey: k)) ?? nil) ?? d }
        church = v(.church, ""); unitType = v(.unitType, "ward"); name = v(.name, ""); stake = v(.stake, "")
        address = v(.address, ""); lat = v(.lat, nil); lng = v(.lng, nil)
        meetingTimes = v(.meetingTimes, [MeetingTime()]); timezone = v(.timezone, TimeZone.current.identifier)
        languages = v(.languages, ["fr"]); email = v(.email, ""); approved = v(.approved, false)
        slug = v(.slug, ""); claim = v(.claim, ""); languagesPicked = v(.languagesPicked, false)
        setupDone = v(.setupDone, false)
    }

    private enum CodingKeys: String, CodingKey {
        case church, unitType, name, stake, address, lat, lng, meetingTimes, timezone, languages, email, approved,
             slug, claim, languagesPicked, setupDone
    }

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

/// Output languages listeners can choose.
struct OutputLanguage: Identifiable, Equatable {
    let id: String
    let name: String
    let native: String        // how its speakers write it, so they can search for it
    let flag: String
    let available: Bool
    let sizeMB: Double        // its voice download (app/packages.py SIZES_MB)
    static let all = [
        OutputLanguage(id: "fr", name: "French", native: "Français", flag: "🇫🇷", available: true, sizeMB: 127),
        OutputLanguage(id: "wo", name: "Wolof", native: "Wolof", flag: "🇸🇳", available: true, sizeMB: 634),
        OutputLanguage(id: "es", name: "Spanish", native: "Español", flag: "🇪🇸", available: true, sizeMB: 127),
        OutputLanguage(id: "ru", name: "Russian", native: "Русский", flag: "🇷🇺", available: true, sizeMB: 127),
        OutputLanguage(id: "pt", name: "Portuguese", native: "Português", flag: "🇧🇷", available: true, sizeMB: 126),
        OutputLanguage(id: "uk", name: "Ukrainian", native: "Українська", flag: "🇺🇦", available: true, sizeMB: 228),
        OutputLanguage(id: "fa", name: "Persian", native: "فارسی", flag: "🇮🇷", available: true, sizeMB: 127),
        OutputLanguage(id: "ne", name: "Nepali", native: "नेपाली", flag: "🇳🇵", available: true, sizeMB: 63),
        OutputLanguage(id: "vi", name: "Vietnamese", native: "Tiếng Việt", flag: "🇻🇳", available: true, sizeMB: 63),
        OutputLanguage(id: "zh", name: "Chinese", native: "中文", flag: "🇨🇳", available: true, sizeMB: 63),
    ]
    /// Not one of the choices: English is translated by itself whenever the speaker isn't speaking it.
    static let english = OutputLanguage(id: "en", name: "English", native: "English", flag: "🇺🇸", available: true, sizeMB: 127)
    static func name(_ code: String) -> String { (all + [english]).first { $0.id == code }?.name ?? code.uppercased() }
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
