// AutoPCBApp.swift
// Responsibility: app entry point; chooses the pair or main screen and checks
// the connection when the app becomes active.
//
// Copyright: GRANDE Robots and Aaron John Sabu

import SwiftUI

/// The AutoPCB application.
@main
struct AutoPCBApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            Group {
                if let link = model.pairing {
                    MainView(link: link)
                } else {
                    PairView()
                }
            }
            .environmentObject(model)
            .onChange(of: scenePhase) { _, phase in
                if phase == .active {
                    Task { await model.recheck() }
                }
            }
        }
    }
}
