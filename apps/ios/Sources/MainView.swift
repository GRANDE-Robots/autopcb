// MainView.swift
// Responsibility: the main screen: the web view, the connection overlays and
// the Forget this computer button.
//
// Copyright: GRANDE Robots and Aaron John Sabu

import SwiftUI

/// Shows the paired computer's page with state overlays.
struct MainView: View {
    @EnvironmentObject private var model: AppModel
    /// The pairing being shown.
    let link: PairingLink

    var body: some View {
        NavigationStack {
            WebView(
                link: link, reloadCount: model.reloadCount,
                onFailure: { model.markConnectionLost() }
            )
            .overlay {
                overlay
            }
            .navigationTitle("AutoPCB")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Forget this computer", role: .destructive) { model.forget() }
                }
            }
        }
    }

    @ViewBuilder
    private var overlay: some View {
        switch model.connection {
        case .online:
            EmptyView()
        case .lost(let text):
            card(text, retry: true)
        case .rejected:
            card(ServerState.tokenRejected.message, retry: false)
        }
    }

    /// A plain message card with optional Retry and Pair again buttons.
    private func card(_ text: String, retry: Bool) -> some View {
        VStack(spacing: 12) {
            Text(text).multilineTextAlignment(.center)
            if retry {
                Button("Retry") { Task { await model.recheck() } }
                    .buttonStyle(.borderedProminent)
            }
            Button("Pair again") { model.forget() }
                .buttonStyle(.bordered)
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.background)
    }
}
