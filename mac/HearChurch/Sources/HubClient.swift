import Foundation

/// hearchurch.com's sign-up API (app/hear/api.py) and the Anthropic key check.
enum HubClient {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// With e-mail verification off (for now) the device token comes back at once; otherwise a claim to poll.
    struct SignUpResult { let slug: String; let deviceToken: String?; let claim: String; let located: Bool }
    enum ClaimResult { case pending; case token(String, slug: String, name: String) }

    static func signUp(_ p: Profile, deviceName: String) async throws -> SignUpResult {
        var body: [String: Any] = [
            "church": p.church, "name": p.name, "unit_type": p.unitType, "email": p.email,
            "address": p.address, "timezone": p.timezone, "device_name": deviceName,
            "meeting_times": p.meetingTimes.map { ["day": $0.day, "time": $0.time] },
        ]
        if !p.stake.isEmpty { body["stake"] = p.stake }
        if let lat = p.lat, let lng = p.lng { body["lat"] = lat; body["lng"] = lng }
        let (json, _) = try await post("/api/hc/signup", body)
        return SignUpResult(slug: json["slug"] as? String ?? "", deviceToken: json["device_token"] as? String,
                            claim: json["claim"] as? String ?? "", located: json["located"] as? Bool ?? false)
    }

    /// Another Mac for a congregation that already exists: e-mails a link, returns the claim to poll.
    static func deviceLink(email: String, slug: String, deviceName: String) async throws -> String {
        let (json, _) = try await post("/api/hc/device-link", ["email": email, "slug": slug, "device_name": deviceName])
        return json["claim"] as? String ?? ""
    }

    static func claim(_ claim: String) async throws -> ClaimResult {
        let (json, status) = try await post("/api/hc/claim", ["claim": claim])
        if status == 202 { return .pending }
        let cong = json["congregation"] as? [String: Any] ?? [:]
        return .token(json["device_token"] as? String ?? "", slug: json["slug"] as? String ?? "",
                      name: cong["name"] as? String ?? "")
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

    /// A free call that proves the key works: list the models it may use.
    static func checkAnthropicKey(_ key: String) async -> Result<Void, Failure> {
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

    private static func post(_ path: String, _ body: [String: Any], token: String? = nil) async throws -> ([String: Any], Int) {
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
