// PairView.swift
// Responsibility: the pair screen: scan the QR code or paste the link, then
// pair and show the result message.
//
// Copyright: GRANDE Robots and Aaron John Sabu

import SwiftUI

/// Collects a pairing link and starts pairing.
struct PairView: View {
    @EnvironmentObject private var model: AppModel
    @State private var linkText = ""
    @State private var isScanning = false

    var body: some View {
        VStack(spacing: 16) {
            Text("Pair with your computer")
                .font(.title2).bold()
            Text("Run autopcb serve on your computer, then scan its QR code or paste its link.")
                .multilineTextAlignment(.center)
            Button("Scan QR code") { isScanning = true }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("scanButton")
            TextField("Pairing link", text: $linkText)
                .textFieldStyle(.roundedBorder)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .accessibilityIdentifier("linkField")
            Button("Pair") { Task { await model.pair(from: linkText) } }
                .buttonStyle(.bordered)
                .disabled(model.isBusy)
                .accessibilityIdentifier("pairButton")
            if model.isBusy {
                ProgressView()
            }
            if let message = model.message {
                Text(message)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .accessibilityIdentifier("pairMessage")
            }
        }
        .padding()
        .sheet(isPresented: $isScanning) {
            ScannerView(
                onCode: { code in
                    isScanning = false
                    linkText = code
                    Task { await model.pair(from: code) }
                },
                onUnavailable: {
                    isScanning = false
                    model.message = "The camera is not available. Paste the link instead."
                })
        }
    }
}
