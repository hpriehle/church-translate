import Foundation

/// hearchurch.com's API for the Mac (app/hear/api.py): sign-in by code, the account, billing, and the API key check.
enum HubClient {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// billing: how the congregation pays, "credits" or "own_key" (its own API key, switched on by Hear Church).
    struct Joined {
        let token: String; let slug: String; let name: String; let church: String; let billing: String
        var unitType = ""; var stake = ""; var address = ""
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
    /// The hub is older than this app and doesn't have that call yet (FastAPI's own 404 for an unknown route).
    /// Nothing is wrong: show it quietly.
    struct NotAvailable: LocalizedError { var errorDescription: String? { "Available after the next update." } }

    private static func missingRoute(_ json: [String: Any], _ status: Int) -> Bool {
        (status == 404 || status == 405) && json["error"] == nil && (json["detail"] as? String).map { $0 == "Not Found" || $0 == "Method Not Allowed" } ?? true
    }

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
        if missingRoute(json, status) { throw NotAvailable() }
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
        if missingRoute(json, status) { throw NotAvailable() }
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

    // MARK: billing (Settings → Billing) and the listener link

    /// A credit bundle: its price, the language-hours it gives, and what an hour comes to.
    struct Bundle: Equatable, Hashable {
        let usd: Int
        let hours: Double
        let usdPerHour: Double
    }

    /// The congregation's saved card, as Stripe describes it (the card itself stays at Stripe).
    struct Card: Equatable {
        let brand: String; let last4: String; let expMonth: Int?; let expYear: Int?
        var label: String {
            let b = brand.isEmpty ? "Card" : brand == "amex" ? "Amex" : brand.prefix(1).uppercased() + brand.dropFirst()
            return last4.isEmpty ? b : "\(b) ••••\(last4)"
        }
        /// "Visa ••4242": in the question before a 1-click purchase.
        var short: String {
            let b = brand.isEmpty ? "Card" : brand == "amex" ? "Amex" : brand.prefix(1).uppercased() + brand.dropFirst()
            return last4.isEmpty ? b : "\(b) ••\(last4)"
        }
    }
    /// Automatic top-up: after a meeting, when the paid hours left are under `belowHours`, buy the `usd` bundle.
    struct AutoTopUp: Equatable { var enabled = false; var usd = 20; var belowHours = 1.0 }

    /// GET /api/hc/billing: how the congregation pays and what it has left.
    struct Billing: Equatable {
        let mode: String               // "credits", "own_key" or "internal" (no charge: no balance, nothing to buy)
        let balanceUSD: Double
        let hoursLeft: Double?         // language-hours: this month's free hour and the paid hours
        let freeLeftHours: Double      // what's left of this month's free hour (resets on the 1st, UTC)
        let paidHoursLeft: Double      // bought hours not used yet (negative when overdrawn)
        let freeResetsAt: Date?
        let outOfCredit: Bool
        let canBuy: Bool
        let bundles: [Bundle]
        let message: String
        var card: Card? = nil          // the saved card (brand, last 4), or none
        var autoTopUp = AutoTopUp()
        var discountPercent: Int? = nil   // a % off code waiting for the next bundle
        var canSaveCard = false        // a hub that knows saved cards (and has Stripe)
        var ownKey: Bool { mode == "own_key" }
        var included: Bool { mode == "internal" }
    }

    static func billing(token: String) async throws -> Billing {
        let (json, status) = try await get("/api/hc/billing", token: token)
        try check(json, status)
        let b = json["balance"] as? [String: Any] ?? [:]
        func num(_ v: Any?) -> Double? { (v as? Double) ?? (v as? Int).map(Double.init) }
        let bundles = (json["bundles"] as? [[String: Any]] ?? []).compactMap { x -> Bundle? in
            guard let usd = x["usd"] as? Int, let hours = num(x["hours"]), hours > 0 else { return nil }
            return Bundle(usd: usd, hours: hours, usdPerHour: num(x["usd_per_hour"]) ?? Double(usd) / hours)
        }
        // a hub from before the bundles: its amounts at its one rate
        let rate = num(json["usd_per_language_hour"]) ?? 5
        let amounts = (json["buy_usd"] as? [Int] ?? []).map { Bundle(usd: $0, hours: Double($0) / max(rate, 0.01), usdPerHour: rate) }
        let free = (num(b["free_left_min"]) ?? 0) / 60
        var out = Billing(mode: json["billing"] as? String ?? "credits", balanceUSD: num(json["balance_usd"]) ?? 0,
                          hoursLeft: num(json["language_hours_left"]), freeLeftHours: free,
                          paidHoursLeft: num(json["paid_hours_left"]) ?? ((num(json["language_hours_left"]) ?? 0) - free),
                          freeResetsAt: num(json["free_resets_at"]).map { Date(timeIntervalSince1970: $0) },
                          outOfCredit: json["out_of_credit"] as? Bool ?? false, canBuy: json["can_buy"] as? Bool ?? false,
                          bundles: bundles.isEmpty ? amounts : bundles, message: json["message"] as? String ?? "")
        if let c = json["card"] as? [String: Any] {
            out.card = Card(brand: c["brand"] as? String ?? "", last4: c["last4"] as? String ?? "",
                            expMonth: c["exp_month"] as? Int, expYear: c["exp_year"] as? Int)
        }
        if let a = json["auto_topup"] as? [String: Any] {
            out.autoTopUp = AutoTopUp(enabled: a["enabled"] as? Bool ?? false, usd: a["usd"] as? Int ?? 20,
                                      belowHours: num(a["below_hours"]) ?? 1)
        }
        out.discountPercent = (json["discount"] as? [String: Any])?["percent"] as? Int
        out.canSaveCard = json["can_save_card"] as? Bool ?? false
        return out
    }

    struct Meeting: Identifiable, Hashable {
        let startedAt: Date; let endedAt: Date?; let languages: [String]; let languageHours: Double
        let freeMinutes: Double; let paidMinutes: Double; let costUSD: Double; let ownKey: Bool
        var included = false
        var id: Date { startedAt }
    }
    struct Charge: Identifiable, Hashable {
        let kind: String; let at: Date; let amountUSD: Double
        var hours: Double? = nil       // the language-hours a top-up gave (a bundle's hours)
        var id: String { "\(kind)\(at.timeIntervalSince1970)" }
    }

    /// GET /api/hc/usage: meetings and their language-hours, and credit bought or refunded, newest first.
    static func usage(token: String) async throws -> (meetings: [Meeting], charges: [Charge]) {
        let (json, status) = try await get("/api/hc/usage", token: token)
        try check(json, status)
        func num(_ v: Any?) -> Double { (v as? Double) ?? (v as? Int).map(Double.init) ?? 0 }
        let meetings = (json["meetings"] as? [[String: Any]] ?? []).map { m in
            Meeting(startedAt: Date(timeIntervalSince1970: num(m["started_at"])),
                    endedAt: (m["ended_at"] as? Double).map { Date(timeIntervalSince1970: $0) },
                    languages: m["languages"] as? [String] ?? [], languageHours: num(m["language_hours"]),
                    freeMinutes: num(m["free_minutes"]), paidMinutes: num(m["paid_minutes"]), costUSD: num(m["cost_usd"]),
                    ownKey: m["billing"] as? String == "own_key", included: m["billing"] as? String == "internal")
        }
        let charges = (json["charges"] as? [[String: Any]] ?? []).map { c in
            Charge(kind: c["kind"] as? String ?? "", at: Date(timeIntervalSince1970: num(c["at"])), amountUSD: num(c["amount_usd"]),
                   hours: c["hours"] == nil ? nil : num(c["hours"]))
        }
        return (meetings, charges)
    }

    /// POST /api/hc/checkout: a Stripe Checkout page for the bundle at that price, opened in the browser.
    /// saveCard: Stripe also keeps the card for 1-click and automatic top-ups.
    static func checkout(usd: Int, saveCard: Bool = false, token: String) async throws -> URL {
        var body: [String: Any] = ["usd": usd]
        if saveCard { body["save_card"] = true }
        let (json, status) = try await send("/api/hc/checkout", body, token: token)
        if status == 503 { throw Failure(message: "Buying credit isn't set up yet. Write to support@hearchurch.com.") }
        try check(json, status)
        guard let s = json["url"] as? String, let url = URL(string: s) else { throw Failure(message: "The payment page didn't open. Try again.") }
        return url
    }

    /// POST /api/hc/card/setup: Stripe's page to save a card (opened in the browser; the card never reaches us).
    static func cardSetup(token: String) async throws -> URL {
        let (json, status) = try await send("/api/hc/card/setup", [:], token: token)
        if status == 503 { throw Failure(message: "Saving a card isn't set up yet. Write to support@hearchurch.com.") }
        try check(json, status)
        guard let s = json["url"] as? String, let url = URL(string: s) else { throw Failure(message: "Stripe's page didn't open. Try again.") }
        return url
    }

    /// POST /api/hc/card/remove: forget the saved card (automatic top-up goes off with it).
    static func removeCard(token: String) async throws {
        let (json, status) = try await send("/api/hc/card/remove", [:], token: token)
        try check(json, status)
    }

    enum TopUpResult { case paid(String); case confirm(URL) }
    struct NeedsCheckout: LocalizedError { let message: String; var errorDescription: String? { message } }

    /// POST /api/hc/topup: 1-click, the saved card pays for the bundle. .confirm(url): the bank wants 3-D Secure on
    /// Stripe's page. NeedsCheckout: no saved card (or the bank wants the full payment page): use Checkout instead.
    static func topUp(usd: Int, requestID: String, token: String) async throws -> TopUpResult {
        let (json, status) = try await send("/api/hc/topup", ["usd": usd, "request_id": requestID], token: token)
        let msg = message(json)
        switch (status, json["error"] as? String) {
        case (200..<300, _):
            if json["status"] as? String == "requires_action", let s = json["url"] as? String, let url = URL(string: s) {
                return .confirm(url)
            }
            return .paid(msg ?? "Payment made. The hours show in a few seconds.")
        case (402, _): throw Failure(message: msg ?? "Your card was declined.")
        case (409, "no_card"), (409, "needs_checkout"): throw NeedsCheckout(message: msg ?? "")
        case (503, _): throw Failure(message: "Buying credit isn't set up yet. Write to support@hearchurch.com.")
        default:
            try check(json, status)
            return .paid(msg ?? "")
        }
    }

    /// POST /api/hc/auto-topup: automatic top-up on or off, and its bundle.
    static func setAutoTopUp(enabled: Bool, usd: Int, token: String) async throws {
        let (json, status) = try await send("/api/hc/auto-topup", ["enabled": enabled, "usd": usd], token: token)
        try check(json, status)
    }

    /// POST /api/hc/codes/redeem: a code from Hear Church (own key, free hours, % off). Returns what it did.
    static func redeemCode(_ code: String, token: String) async throws -> String {
        let (json, status) = try await send("/api/hc/codes/redeem", ["code": code], token: token)
        switch status {
        case 200..<300:
            if json["already"] as? Bool == true { return "You already used this code." }
            return json["message"] as? String ?? "Code applied."
        case 403 where json["error"] as? String == "bad_code": throw Failure(message: message(json) ?? "That code isn't valid.")
        case 409: throw Failure(message: message(json) ?? "That code can't be used right now.")
        case 429: throw tooManyTries(json)
        default:
            try check(json, status)
            return ""
        }
    }

    /// POST /api/hc/web/link: a one-time link (2 minutes) that opens hearchurch.com/account signed in, on Billing.
    /// An older hub without it: the plain account page (sign in there by e-mailed code).
    static func accountLink(token: String) async -> URL {
        let plain = AppPaths.hub.appendingPathComponent("account")
        guard let answer = try? await send("/api/hc/web/link", [:], token: token), (200..<300).contains(answer.1),
              let s = answer.0["url"] as? String, let url = URL(string: s), ["https", "http"].contains(url.scheme ?? "") else { return plain }
        return url
    }

    /// POST /api/hc/slug: the listener link becomes hearchurch.com/<slug>; the old one keeps redirecting.
    static func changeSlug(_ slug: String, token: String) async throws -> String {
        let (json, status) = try await send("/api/hc/slug", ["slug": slug], token: token)
        try check(json, status)
        return json["slug"] as? String ?? slug
    }

    /// POST /api/hc/logs (kind "manual"): "Send logs to support". `lines` come from the scrubbed diagnostic log the
    /// engine writes (DiagnosticLog: technical only), newest kept when there are too many; `facts` is one more line
    /// about the app. Returns the reference the hub gives it.
    static func sendLogs(_ lines: [[String: Any]], facts: String, note: String, token: String) async throws -> Int {
        var kept: [[String: Any]] = [["t": Date().timeIntervalSince1970, "lvl": "INFO", "src": "app", "msg": facts]]
        var size = 0
        for line in lines.reversed() {   // newest first, up to ~900 KB (the hub takes 1 MB) and 4,000 lines
            let n = ((try? JSONSerialization.data(withJSONObject: line))?.count ?? 0) + 1
            if size + n > 900_000 || kept.count >= 4000 { break }
            size += n
            kept.append(line)
        }
        let body: [String: Any] = ["kind": "manual", "lines": Array(kept.reversed()), "note": String(note.prefix(1000))]
        let (json, status) = try await send("/api/hc/logs", body, token: token)
        try check(json, status)
        guard let ref = json["ref"] as? Int else { throw Failure(message: "The logs were not stored. Try again later.") }
        return ref
    }

    /// Throws for anything but a 2xx: NotAvailable (older hub), SignedOut (403), or the hub's message.
    private static func check(_ json: [String: Any], _ status: Int) throws {
        if (200..<300).contains(status) { return }
        if missingRoute(json, status) { throw NotAvailable() }
        if status == 403 { throw SignedOut() }
        if status == 429 { throw Failure(message: message(json) ?? "Too many tries. Try again later.") }
        throw Failure(message: message(json) ?? "Server error \(status).")
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
    static func checkAPIKey(_ key: String) async -> Result<Void, Failure> {
        // A subscription token can't be checked against the API; the engine reports any problem with it.
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
            case 401: return .failure(Failure(message: "This key was not accepted. Copy it again from the console."))
            case 403: return .failure(Failure(message: "This key is not allowed to use the API (check the workspace)."))
            case let code: return .failure(Failure(message: "The key check answered \(code). Try again in a minute."))
            }
        } catch {
            return .failure(Failure(message: "Cannot check the key right now. Is the internet connected?"))
        }
    }

    // MARK: -

    /// Tells the hub this app knows the "internal" billing type (older apps are told "credits" instead).
    private static let billingTypes = "credits,own_key,internal"

    private static func send(_ path: String, _ body: [String: Any], token: String? = nil) async throws -> ([String: Any], Int) {
        var req = URLRequest(url: AppPaths.hub.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.timeoutInterval = 30
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        req.setValue(billingTypes, forHTTPHeaderField: "X-HC-Billing-Types")
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
        req.setValue(billingTypes, forHTTPHeaderField: "X-HC-Billing-Types")
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
