import Foundation
import Security

/// The Anthropic API key and the hearchurch.com device token, as generic passwords in the login Keychain.
enum Keychain {
    static let service = "com.hearchurch.HearChurch"

    enum Item: String {
        case apiKey = "anthropic-api-key"
        case deviceToken = "device-token"
        case connectCode = "connect-code"
        case ownKeyCode = "own-key-code"   // checked during setup, used up once the congregation exists
    }

    static func get(_ item: Item) -> String? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: service,
                                kSecAttrAccount as String: item.rawValue,
                                kSecReturnData as String: true,
                                kSecMatchLimit as String: kSecMatchLimitOne]
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    @discardableResult
    static func set(_ item: Item, _ value: String) -> Bool {
        delete(item)
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: service,
                                kSecAttrAccount as String: item.rawValue,
                                kSecAttrLabel as String: "Hear Church (\(item.rawValue))",
                                kSecValueData as String: Data(value.utf8),
                                kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock]
        return SecItemAdd(q as CFDictionary, nil) == errSecSuccess
    }

    static func delete(_ item: Item) {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: service,
                                kSecAttrAccount as String: item.rawValue]
        SecItemDelete(q as CFDictionary)
    }
}
