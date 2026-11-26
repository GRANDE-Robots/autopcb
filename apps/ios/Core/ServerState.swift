// ServerState.swift
// Responsibility: classify the result of the `/api/info` check into one of the
// states the app shows. Pure logic, no network access.
//
// Copyright: GRANDE Robots and Aaron John Sabu

import Foundation

/// What the app learned about the server behind a pairing link.
enum ServerState: Equatable {
    /// The request failed or timed out.
    case unreachable
    /// A server answered but is not an AutoPCB server speaking API level 1.
    case notAutoPCB
    /// An AutoPCB server answered and refused the token.
    case tokenRejected
    /// An AutoPCB server answered and accepted the token.
    case ready

    /// The plain message shown to the user for this state; empty for `ready`.
    var message: String {
        switch self {
        case .unreachable:
            return "Cannot reach the computer. Check that autopcb serve is running "
                + "and that this phone is on the same network."
        case .notAutoPCB:
            return "That address is not an AutoPCB server this app supports."
        case .tokenRejected:
            return "The computer rejected the access token. Pair again with the "
                + "current link."
        case .ready:
            return ""
        }
    }
}

/// Maps an `/api/info` response, or the error that replaced it, to a state.
struct InfoClassifier {
    /// The JSON fields of `/api/info` that the app reads.
    private struct Info: Decodable {
        let product: String?
        let api: Int?
        let authorized: Bool?
    }

    /// Classifies one `/api/info` attempt.
    ///
    /// Inputs: data, the response body, if any; error, the failure, if any.
    /// Outputs: `unreachable` for any error or missing body, `notAutoPCB` for a
    /// body that is not JSON or has product other than "AutoPCB" or api other
    /// than 1, `tokenRejected` for authorized false, otherwise `ready`.
    func callAsFunction(data: Data?, error: Error?) -> ServerState {
        guard error == nil, let data = data else { return .unreachable }
        guard let info = try? JSONDecoder().decode(Info.self, from: data),
            info.product == "AutoPCB", info.api == 1
        else { return .notAutoPCB }
        return info.authorized == true ? .ready : .tokenRejected
    }
}
