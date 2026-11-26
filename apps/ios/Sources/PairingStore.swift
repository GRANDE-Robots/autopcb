// PairingStore.swift
// Responsibility: keep the paired origin and token in the Keychain, never in
// UserDefaults or logs.
//
// Copyright: GRANDE Robots and Aaron John Sabu

import Foundation
import Security

/// Reads, writes and deletes the single stored pairing.
struct PairingStore {
    private let service = "com.grande.autopcb"
    private let account = "pairing"

    private var identity: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    /// Stores the pairing, replacing any earlier one.
    ///
    /// Inputs: link, the pairing to keep, encoded as JSON in one Keychain item
    /// readable after first unlock on this device only.
    /// Outputs: true when the Keychain accepted the item.
    func save(_ link: PairingLink) -> Bool {
        guard let data = try? JSONEncoder().encode(link) else { return false }
        SecItemDelete(identity as CFDictionary)
        var item = identity
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(item as CFDictionary, nil) == errSecSuccess
    }

    /// Reads the stored pairing.
    ///
    /// Outputs: the pairing, or nil when none is stored or it cannot be decoded.
    func load() -> PairingLink? {
        var query = identity
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
            let data = result as? Data
        else { return nil }
        return try? JSONDecoder().decode(PairingLink.self, from: data)
    }

    /// Deletes the stored pairing; does nothing when none is stored.
    func clear() {
        SecItemDelete(identity as CFDictionary)
    }
}
