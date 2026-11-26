// WebView.swift
// Responsibility: WKWebView wrapper that loads the AutoPCB page for the paired
// origin and refuses to navigate anywhere else.
//
// Copyright: GRANDE Robots and Aaron John Sabu

import SwiftUI
import WebKit

/// Shows the AutoPCB web page of the paired computer.
struct WebView: UIViewRepresentable {
    /// The pairing whose page is shown.
    let link: PairingLink
    /// The page reloads whenever this value changes.
    let reloadCount: Int
    /// Called when a navigation fails.
    let onFailure: () -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(origin: link.origin, onFailure: onFailure)
    }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        context.coordinator.load(link, reloadCount: reloadCount, in: view)
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {
        context.coordinator.onFailure = onFailure
        if context.coordinator.loadedCount != reloadCount {
            context.coordinator.load(link, reloadCount: reloadCount, in: view)
        }
    }

    /// Navigation delegate that locks the view to one origin.
    final class Coordinator: NSObject, WKNavigationDelegate {
        private let origin: Origin
        var onFailure: () -> Void
        /// The reload count of the last load request.
        private(set) var loadedCount = 0

        init(origin: Origin, onFailure: @escaping () -> Void) {
            self.origin = origin
            self.onFailure = onFailure
        }

        /// Loads the login URL and remembers which reload count it answers.
        func load(_ link: PairingLink, reloadCount: Int, in view: WKWebView) {
            loadedCount = reloadCount
            view.load(URLRequest(url: link.loginURL))
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            if let url = navigationAction.request.url, origin.matches(url) {
                decisionHandler(.allow)
            } else {
                decisionHandler(.cancel)
            }
        }

        func webView(
            _ webView: WKWebView,
            didFailProvisionalNavigation navigation: WKNavigation!,
            withError error: Error
        ) {
            report(error)
        }

        func webView(
            _ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error
        ) {
            report(error)
        }

        /// Reports a failure unless it is a cancelled navigation.
        private func report(_ error: Error) {
            if (error as NSError).code != NSURLErrorCancelled {
                onFailure()
            }
        }
    }
}
