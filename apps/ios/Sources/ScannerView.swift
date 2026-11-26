// ScannerView.swift
// Responsibility: camera QR scanner for SwiftUI, built on AVFoundation.
//
// Copyright: GRANDE Robots and Aaron John Sabu

import AVFoundation
import SwiftUI

/// Shows the camera and reports the first QR code it reads.
struct ScannerView: UIViewControllerRepresentable {
    /// Called once with the text of the first QR code.
    let onCode: (String) -> Void
    /// Called when camera access is denied or no camera exists.
    let onUnavailable: () -> Void

    func makeUIViewController(context: Context) -> ScannerController {
        let controller = ScannerController()
        controller.onCode = onCode
        controller.onUnavailable = onUnavailable
        return controller
    }

    func updateUIViewController(_ controller: ScannerController, context: Context) {}
}

/// View controller that runs the capture session with a QR metadata output.
final class ScannerController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    /// Receives the first QR code text.
    var onCode: ((String) -> Void)?
    /// Told when the camera cannot be used.
    var onUnavailable: (() -> Void)?

    private let session = AVCaptureSession()
    private var previewLayer: AVCaptureVideoPreviewLayer?
    private var hasDelivered = false

    override func viewDidLoad() {
        super.viewDidLoad()
        Task { await startSession() }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        previewLayer?.frame = view.bounds
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        let session = self.session
        DispatchQueue.global(qos: .userInitiated).async { session.stopRunning() }
    }

    /// Asks for camera access, wires input and QR output, and starts capture.
    private func startSession() async {
        guard await AVCaptureDevice.requestAccess(for: .video),
            let device = AVCaptureDevice.default(for: .video),
            let input = try? AVCaptureDeviceInput(device: device),
            session.canAddInput(input)
        else {
            onUnavailable?()
            return
        }
        session.addInput(input)
        let output = AVCaptureMetadataOutput()
        guard session.canAddOutput(output) else {
            onUnavailable?()
            return
        }
        session.addOutput(output)
        output.setMetadataObjectsDelegate(self, queue: .main)
        output.metadataObjectTypes = [.qr]
        let layer = AVCaptureVideoPreviewLayer(session: session)
        layer.videoGravity = .resizeAspectFill
        layer.frame = view.bounds
        view.layer.addSublayer(layer)
        previewLayer = layer
        let session = self.session
        DispatchQueue.global(qos: .userInitiated).async { session.startRunning() }
    }

    func metadataOutput(
        _ output: AVCaptureMetadataOutput,
        didOutput metadataObjects: [AVMetadataObject],
        from connection: AVCaptureConnection
    ) {
        guard !hasDelivered,
            let code = metadataObjects.compactMap({
                ($0 as? AVMetadataMachineReadableCodeObject)?.stringValue
            }).first
        else { return }
        hasDelivered = true
        onCode?(code)
    }
}
