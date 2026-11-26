// AppModel.swift
// Responsibility: app state shared by the screens: the stored pairing, the
// pairing attempt, and the connection state shown over the web view.
//
// Copyright: GRANDE Robots and Aaron John Sabu

import Foundation

/// How the paired computer looks from the phone.
enum Connection: Equatable {
    /// The server answered and accepted the token.
    case online
    /// The server cannot be used; the text says why.
    case lost(String)
    /// The server refused the stored token.
    case rejected
}

/// Holds the pairing and connection state and performs pairing.
@MainActor
final class AppModel: ObservableObject {
    /// The stored pairing, or nil when the pair screen should show.
    @Published private(set) var pairing: PairingLink?
    /// The connection state of the paired computer.
    @Published private(set) var connection: Connection = .online
    /// Incremented whenever the web view must load the page again.
    @Published private(set) var reloadCount = 0
    /// The message shown on the pair screen, if any.
    @Published var message: String?
    /// True while a pairing attempt is waiting for the server.
    @Published private(set) var isBusy = false

    private let store = PairingStore()
    private let probe = ServerProbe()

    /// Loads the stored pairing, if any.
    init() {
        pairing = store.load()
    }

    /// Pairs from link text and stores the pairing only when the server is ready.
    ///
    /// Inputs: text, a pasted or scanned pairing link.
    /// Outputs: sets `pairing` on success, otherwise sets `message`.
    func pair(from text: String) async {
        guard let link = LinkParser()(text) else {
            message = "That is not a valid AutoPCB pairing link."
            return
        }
        isBusy = true
        let state = await probe(link)
        isBusy = false
        guard state == .ready else {
            message = state.message
            return
        }
        guard store.save(link) else {
            message = "Could not save the pairing in the Keychain."
            return
        }
        message = nil
        connection = .online
        pairing = link
    }

    /// Checks the paired server again and updates the connection state.
    ///
    /// Outputs: a transition to online makes the web view reload.
    func recheck() async {
        guard let link = pairing else { return }
        let state = await probe(link)
        switch state {
        case .ready:
            if connection != .online {
                connection = .online
                reloadCount += 1
            }
        case .tokenRejected:
            connection = .rejected
        case .unreachable, .notAutoPCB:
            connection = .lost(state.message)
        }
    }

    /// Records that the web view could not load the page.
    func markConnectionLost() {
        connection = .lost(ServerState.unreachable.message)
    }

    /// Clears the Keychain and returns to the pair screen.
    func forget() {
        store.clear()
        pairing = nil
        message = nil
    }
}
