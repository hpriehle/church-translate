import Foundation

/// hearchurch.com's sign-up API (app/hear/api.py) and the Anthropic key check.
enum HubClient {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// With e-mail verification off (for now) the device token comes back at once; otherwise a claim to poll.
    struct SignUpResult { let slug: String; let deviceToken: String?; let claim: String; let located: Bool; let connectCode: String? }
    /// billing: how the congregation pays, "credits" or "own_key" (its own Anthropic key, through a one-time code).
    struct Joined {
        let token: String; let slug: String; let name: String; let church: String; let billing: String
        var unitType = ""; var stake = ""; var address = ""
    }

    static func signUp(_ p: Profile, deviceName: String) async throws -> SignUpResult {
        var body: [String: Any] = [
            "church": p.church, "name": p.name, "unit_type": p.unitType, "email": p.email,
            "address": p.address, "timezone": p.timezone, "device_name": deviceName,
            "meeting_times": p.meetingTimes.map { ["day": $0.day, "time": $0.time] },
        ]
        if !p.stake.isEmpty { body["stake"] = p.stake }
        let more = p.extraEmails.map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty && $0 != p.email }
        if !more.isEmpty { body["emails"] = more }
        if let lat = p.lat, let lng = p.lng { body["lat"] = lat; body["lng"] = lng }
        let (json, _) = try await post("/api/hc/signup", body)
        return SignUpResult(slug: json["slug"] as? String ?? "", deviceToken: json["device_token"] as? String,
                            claim: json["claim"] as? String ?? "", located: json["located"] as? Bool ?? false,
                            connectCode: json["connect_code"] as? String)
    }

    /// "Connect by code": a code from "Add device" on another Mac, or from the e-mail (POST /api/hc/pair).
    static func pair(code: String, deviceName: String) async throws -> Joined {
        let (json, status) = try await send("/api/hc/pair", ["code": code, "device_name": deviceName])
        switch status {
        case 200..<300:
            let cong = json["congregation"] as? [String: Any] ?? [:]
            let token = json["device_token"] as? String ?? ""
            guard !token.isEmpty else { throw Failure(message: "The server did not send a device token. Try again.") }
            return Joined(token: token, slug: json["slug"] as? String ?? "", name: cong["name"] as? String ?? "",
                          church: cong["church"] as? String ?? "", billing: json["billing"] as? String ?? "credits",
                          unitType: cong["unit_type"] as? String ?? "", stake: cong["stake"] as? String ?? "",
                          address: cong["address"] as? String ?? "")
        case 403 where json["error"] as? String == "bad_code":
            throw Failure(message: "That code is wrong or has expired. Ask for a new one: codes work once, for 15 minutes.")
        case 429: throw tooManyTries(json)
        default: throw Failure(message: message(json) ?? "Server error \(status).")
        }
    }

    /// "Email me a code": the hub sends a code to every address of the congregation this e-mail belongs to (and says
    /// the same whether or not it does). Returns the hub's sentence.
    static func emailCode(_ email: String) async throws -> String {
        let (json, status) = try await send("/api/hc/email-code", ["email": email])
        switch status {
        case 200..<300: return json["message"] as? String ?? "If that e-mail belongs to a congregation, a code is on its way."
        case 503: throw Failure(message: "Hear Church can't send e-mail yet. On a Mac that is already connected, open Settings → Congregation → Add device, and enter the code it shows.")
        case 422: throw Failure(message: "That doesn't look like an e-mail address.")
        case 429: throw Failure(message: "Too many codes asked for. Wait an hour, or use Add device on a connected Mac.")
        default: throw Failure(message: message(json) ?? "Server error \(status).")
        }
    }

    // MARK: the congregation's account (Settings): e-mails, Macs, "Add device"

    struct Email: Identifiable, Hashable { let email: String; let main: Bool; var id: String { email } }
    struct Device: Identifiable, Hashable {
        let id: Int; let name: String; let createdAt: Date?; let lastSeen: Date?; let appVersion: String; let this: Bool
    }
    struct Account { let mainEmail: String; let emails: [Email]; let devices: [Device] }
    /// The hub no longer knows this Mac's token (revoked, or the congregation is gone): set up again.
    struct SignedOut: LocalizedError { var errorDescription: String? { "This Mac was disconnected from the congregation." } }

    static func account(token: String) async throws -> Account {
        try accountAnswer(try await get("/api/hc/account", token: token))
    }
    static func addEmail(_ email: String, main: Bool = false, token: String) async throws -> Account {
        try accountAnswer(try await send("/api/hc/emails", ["email": email, "main": main], token: token))
    }
    static func setMainEmail(_ email: String, token: String) async throws -> Account {
        try accountAnswer(try await send("/api/hc/emails/main", ["email": email], token: token))
    }
    static func removeEmail(_ email: String, token: String) async throws -> Account {
        try accountAnswer(try await send("/api/hc/emails/remove", ["email": email], token: token))
    }
    static func revokeDevice(_ id: Int, token: String) async throws -> Account {
        try accountAnswer(try await send("/api/hc/devices/revoke", ["id": id], token: token))
    }

    /// "Add device": a one-time code another Mac enters, and when it stops working.
    static func pairingCode(token: String) async throws -> (code: String, expires: Date) {
        let (json, status) = try await send("/api/hc/pairing-code", [:], token: token)
        if status == 403 { throw SignedOut() }
        if status == 429 { throw Failure(message: "Too many codes this hour. Try again later.") }
        guard (200..<300).contains(status), let code = json["code"] as? String else {
            throw Failure(message: message(json) ?? "Server error \(status).")
        }
        let secs = (json["expires_in"] as? Double) ?? (json["expires_in"] as? Int).map(Double.init) ?? 900
        return (code, Date().addingTimeInterval(secs))
    }

    private static func accountAnswer(_ answer: ([String: Any], Int)) throws -> Account {
        let (json, status) = answer
        switch status {
        case 200..<300: break
        case 403: throw SignedOut()
        case 422: throw Failure(message: "That doesn't look like an e-mail address.")
        default: throw Failure(message: message(json) ?? "Server error \(status).")
        }
        func date(_ v: Any?) -> Date? { (v as? Double).map { Date(timeIntervalSince1970: $0) } }
        let emails = (json["emails"] as? [[String: Any]] ?? []).compactMap { e -> Email? in
            guard let a = e["email"] as? String else { return nil }
            return Email(email: a, main: e["main"] as? Bool ?? false)
        }
        let devices = (json["devices"] as? [[String: Any]] ?? []).compactMap { d -> Device? in
            guard let id = d["id"] as? Int else { return nil }
            return Device(id: id, name: d["name"] as? String ?? "Mac", createdAt: date(d["created_at"]), lastSeen: date(d["last_seen"]),
                          appVersion: d["app_version"] as? String ?? "", this: d["this"] as? Bool ?? false)
        }
        return Account(mainEmail: json["main_email"] as? String ?? emails.first { $0.main }?.email ?? "", emails: emails, devices: devices)
    }

    /// Hub errors are {"error": code, "message": text}, or FastAPI's {"detail": …}.
    private static func message(_ json: [String: Any]) -> String? { json["message"] as? String ?? detail(json) }

    /// "7kqm x2pa" → "7KQM-X2PA" while typing: letters and digits only, at most 8.
    static func pairingFormat(_ text: String) -> String {
        let chars = String(text.uppercased().filter { $0.isASCII && ($0.isLetter || $0.isNumber) }.prefix(8))
        return chars.count > 4 ? String(chars.prefix(4)) + "-" + String(chars.dropFirst(4)) : chars
    }

    private static func tooManyTries(_ json: [String: Any]) -> Failure {
        let secs = (json["retry_after"] as? Double) ?? (json["retry_after"] as? Int).map(Double.init) ?? 60
        let mins = max(1, Int((secs / 60).rounded(.up)))
        return Failure(message: "Too many tries. Try again in \(mins) minute\(mins == 1 ? "" : "s").")
    }

    /// Is this an unused one-time own-key code (from Harrison, for one church)? Nothing is used up: the
    /// congregation doesn't exist yet. Throws with a message to show when it isn't.
    static func checkOwnKeyCode(_ code: String) async throws {
        let (json, status) = try await send("/api/hc/own-key-code/check", ["code": code])
        try ownKeyAnswer(json, status)
    }

    /// Use the code up for this Mac's congregation: it pays with its own Anthropic key from now on.
    static func redeemOwnKeyCode(_ code: String, token: String) async throws {
        let (json, status) = try await send("/api/hc/own-key-code", ["code": code], token: token)
        try ownKeyAnswer(json, status)
    }

    private static func ownKeyAnswer(_ json: [String: Any], _ status: Int) throws {
        switch status {
        case 200..<300: return
        case 403 where json["error"] as? String == "bad_code":
            throw Failure(message: "That code doesn't work. Check it, or write to support@hearchurch.com. Each code works once.")
        case 429: throw tooManyTries(json)
        default: throw Failure(message: detail(json) ?? "Server error \(status).")
        }
    }

    static func setLocation(lat: Double, lng: Double, address: String, token: String) async throws {
        _ = try await post("/api/hc/location", ["lat": lat, "lng": lng, "address": address], token: token)
    }

    /// Is the hub reachable at all (preflight)?
    static func reachable() async -> Bool {
        var req = URLRequest(url: AppPaths.hub.appendingPathComponent("health"))
        req.timeoutInterval = 8
        guard let (_, resp) = try? await URLSession.shared.data(for: req) else { return false }
        return (resp as? HTTPURLResponse)?.statusCode == 200
    }

    static func isSubscriptionToken(_ key: String) -> Bool { key.hasPrefix("sk-ant-oat") }

    /// A free call that proves the key works: list the models it may use.
    static func checkAnthropicKey(_ key: String) async -> Result<Void, Failure> {
        // A subscription token (from `claude setup-token`) can't be checked against the API; Claude Code
        // uses it, and the engine reports any problem with it.
        if isSubscriptionToken(key) {
            return key.count >= 40 && key.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" })
                ? .success(()) : .failure(Failure(message: "That token looks incomplete. Copy it again."))
        }
        var req = URLRequest(url: URL(string: "https://api.anthropic.com/v1/models?limit=1")!)
        req.setValue(key, forHTTPHeaderField: "x-api-key")
        req.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
        req.timeoutInterval = 15
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            switch (resp as? HTTPURLResponse)?.statusCode ?? 0 {
            case 200: return .success(())
            case 401: return .failure(Failure(message: "Anthropic did not accept this key. Copy it again from the console."))
            case 403: return .failure(Failure(message: "This key is not allowed to use the API (check the workspace)."))
            case let code: return .failure(Failure(message: "Anthropic answered \(code). Try again in a minute."))
            }
        } catch {
            return .failure(Failure(message: "Cannot reach Anthropic. Is the internet connected?"))
        }
    }

    // MARK: -

    private static func send(_ path: String, _ body: [String: Any], token: String? = nil) async throws -> ([String: Any], Int) {
        var req = URLRequest(url: AppPaths.hub.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.timeoutInterval = 30
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let data: Data, resp: URLResponse
        do {
            (data, resp) = try await URLSession.shared.data(for: req)
        } catch {
            throw Failure(message: "Cannot reach \(AppPaths.hub.host ?? "hearchurch.com"). Is the internet connected?")
        }
        let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        return (json, status)
    }

    private static func get(_ path: String, token: String) async throws -> ([String: Any], Int) {
        var req = URLRequest(url: AppPaths.hub.appendingPathComponent(path))
        req.timeoutInterval = 30
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let data: Data, resp: URLResponse
        do {
            (data, resp) = try await URLSession.shared.data(for: req)
        } catch {
            throw Failure(message: "Cannot reach \(AppPaths.hub.host ?? "hearchurch.com"). Is the internet connected?")
        }
        return ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:], (resp as? HTTPURLResponse)?.statusCode ?? 0)
    }

    private static func post(_ path: String, _ body: [String: Any], token: String? = nil) async throws -> ([String: Any], Int) {
        let (json, status) = try await send(path, body, token: token)
        guard (200..<300).contains(status) else { throw Failure(message: detail(json) ?? "Server error \(status).") }
        return (json, status)
    }

    /// FastAPI errors: {"detail": "text"} or {"detail": [{"msg": …, "loc": [...]}, …]} for validation.
    private static func detail(_ json: [String: Any]) -> String? {
        if let s = json["detail"] as? String { return s }
        if let list = json["detail"] as? [[String: Any]] {
            return list.compactMap { e -> String? in
                let field = (e["loc"] as? [Any])?.last.map { "\($0)".replacingOccurrences(of: "_", with: " ") }
                guard let msg = e["msg"] as? String else { return nil }
                return field.map { "\($0): \(msg)" } ?? msg
            }.joined(separator: "\n")
        }
        return nil
    }
}
