// PairingLink.swift
// Responsibility: the paired server's origin and access token, and the parser
// that reads them from the link printed by `autopcb serve`. Pure logic, no I/O.
//
// Copyright: GRANDE Robots and Aaron John Sabu

import Foundation

/// The scheme, host and port that identify one `autopcb serve` instance.
struct Origin: Codable, Equatable {
    /// URL scheme in lower case; only "http" is ever stored.
    let scheme: String
    /// Host name or address in lower case, without IPv6 brackets.
    let host: String
    /// TCP port; 80 when the link named none.
    let port: Int

    /// The origin as a URL with no path, for example `http://192.168.1.5:8765`.
    var url: URL {
        let hostText = host.contains(":") ? "[\(host)]" : host
        return URL(string: "\(scheme)://\(hostText):\(port)")!
    }

    /// Says whether a URL has this origin's scheme, host and port.
    ///
    /// Inputs: url, any URL, including one without a host.
    /// Outputs: true only when all three parts are equal.
    func matches(_ url: URL) -> Bool {
        return url.scheme?.lowercased() == scheme
            && url.host?.lowercased() == host
            && (url.port ?? 80) == port
    }
}

/// An origin together with the bearer token that authorizes API calls to it.
struct PairingLink: Codable, Equatable {
    /// The server this link points to.
    let origin: Origin
    /// The access token from the link fragment.
    let token: String

    /// The URL that logs the AutoPCB web page in: `{origin}/#token=TOKEN`.
    var loginURL: URL {
        var components = URLComponents(url: origin.url, resolvingAgainstBaseURL: false)
        components?.path = "/"
        components?.fragment = "token=\(token)"
        return components?.url ?? origin.url
    }
}

/// Reads a pairing link of the form `http://HOST:PORT/#token=TOKEN`.
struct LinkParser {
    /// Parses pasted or scanned text into a pairing link.
    ///
    /// Inputs: text, the link; surrounding whitespace and extra fragment
    /// parameters are ignored.
    /// Outputs: the link, or nil when the scheme is not http, the host is
    /// missing, or the fragment has no non-empty `token`.
    func callAsFunction(_ text: String) -> PairingLink? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let components = URLComponents(string: trimmed),
            components.scheme?.lowercased() == "http",
            let host = components.host, !host.isEmpty,
            let fragment = components.fragment
        else { return nil }
        let tokens = fragment.split(separator: "&").compactMap { pair -> String? in
            let parts = pair.split(separator: "=", maxSplits: 1)
            return parts.count == 2 && parts[0] == "token" ? String(parts[1]) : nil
        }
        guard let token = tokens.first else { return nil }
        let origin = Origin(
            scheme: "http", host: host.lowercased(), port: components.port ?? 80)
        return PairingLink(origin: origin, token: token)
    }
}
