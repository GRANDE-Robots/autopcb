// ServerProbe.swift
// Responsibility: ask `/api/info` of a paired server with the bearer token and
// return the classified state. The only network call the app makes itself.
//
// Copyright: GRANDE Robots and Aaron John Sabu

import Foundation

/// Calls `GET /api/info` and classifies the answer.
struct ServerProbe {
    /// Seconds to wait before treating the server as unreachable.
    static let timeoutSeconds: TimeInterval = 5

    /// Checks one server.
    ///
    /// Inputs: link, the origin and token to check.
    /// Outputs: the classified state; every failure, including a timeout, is
    /// `unreachable`. The token is sent only in the Authorization header.
    func callAsFunction(_ link: PairingLink) async -> ServerState {
        var request = URLRequest(
            url: link.origin.url.appendingPathComponent("api/info"),
            timeoutInterval: Self.timeoutSeconds)
        request.setValue("Bearer \(link.token)", forHTTPHeaderField: "Authorization")
        do {
            let (data, _) = try await URLSession.shared.data(for: request)
            return InfoClassifier()(data: data, error: nil)
        } catch {
            return InfoClassifier()(data: nil, error: error)
        }
    }
}
