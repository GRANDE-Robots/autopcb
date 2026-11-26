// InfoClassifierTests.swift
// Responsibility: unit tests for the /api/info classifier, one per state.
//
// Copyright: GRANDE Robots and Aaron John Sabu

import XCTest

final class InfoClassifierTests: XCTestCase {
    private func classify(_ json: String) -> ServerState {
        return InfoClassifier()(data: json.data(using: .utf8), error: nil)
    }

    func testAuthorizedAutoPCBServerIsReady() {
        let json = #"{"product":"AutoPCB","version":"1.0","api":1,"authorized":true}"#
        XCTAssertEqual(classify(json), .ready)
    }

    func testUnauthorizedAutoPCBServerIsTokenRejected() {
        let json = #"{"product":"AutoPCB","version":"1.0","api":1,"authorized":false}"#
        XCTAssertEqual(classify(json), .tokenRejected)
    }

    func testOtherProductIsNotAutoPCB() {
        let json = #"{"product":"Other","api":1,"authorized":true}"#
        XCTAssertEqual(classify(json), .notAutoPCB)
    }

    func testUnsupportedApiLevelIsNotAutoPCB() {
        let json = #"{"product":"AutoPCB","api":2,"authorized":true}"#
        XCTAssertEqual(classify(json), .notAutoPCB)
    }

    func testBodyThatIsNotJSONIsNotAutoPCB() {
        XCTAssertEqual(classify("<html></html>"), .notAutoPCB)
    }

    func testURLErrorIsUnreachable() {
        XCTAssertEqual(
            InfoClassifier()(data: nil, error: URLError(.cannotConnectToHost)), .unreachable)
    }

    func testTimeoutIsUnreachable() {
        XCTAssertEqual(
            InfoClassifier()(data: nil, error: URLError(.timedOut)), .unreachable)
    }

    func testMissingBodyWithoutErrorIsUnreachable() {
        XCTAssertEqual(InfoClassifier()(data: nil, error: nil), .unreachable)
    }

    func testEveryFailureStateHasAMessage() {
        for state in [ServerState.unreachable, .notAutoPCB, .tokenRejected] {
            XCTAssertFalse(state.message.isEmpty)
        }
    }
}
