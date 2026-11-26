// LinkParserTests.swift
// Responsibility: unit tests for the pairing link parser and origin matching.
//
// Copyright: GRANDE Robots and Aaron John Sabu

import XCTest

final class LinkParserTests: XCTestCase {
    func testValidLinkGivesOriginAndToken() {
        let link = LinkParser()("http://192.168.1.5:8765/#token=abc123")
        XCTAssertEqual(link?.origin, Origin(scheme: "http", host: "192.168.1.5", port: 8765))
        XCTAssertEqual(link?.token, "abc123")
    }

    func testMissingTokenIsRejected() {
        XCTAssertNil(LinkParser()("http://192.168.1.5:8765/"))
        XCTAssertNil(LinkParser()("http://192.168.1.5:8765/#"))
        XCTAssertNil(LinkParser()("http://192.168.1.5:8765/#token="))
    }

    func testMissingPortDefaultsTo80() {
        XCTAssertEqual(LinkParser()("http://example.test/#token=t")?.origin.port, 80)
    }

    func testWrongSchemeIsRejected() {
        XCTAssertNil(LinkParser()("https://192.168.1.5:8765/#token=abc"))
        XCTAssertNil(LinkParser()("ftp://192.168.1.5:8765/#token=abc"))
        XCTAssertNil(LinkParser()("192.168.1.5:8765/#token=abc"))
    }

    func testTrailingWhitespaceAndExtraParametersAreIgnored() {
        let link = LinkParser()("  http://h.test:9/#token=abc&other=1\n")
        XCTAssertEqual(link?.token, "abc")
        XCTAssertEqual(link?.origin.port, 9)
    }

    func testLoginURLCarriesTokenInFragmentOnly() {
        let link = LinkParser()("http://h.test:9/#token=abc")
        XCTAssertEqual(link?.loginURL.absoluteString, "http://h.test:9/#token=abc")
    }

    func testOriginMatchesOnlySameSchemeHostAndPort() {
        let origin = Origin(scheme: "http", host: "h.test", port: 9)
        XCTAssertTrue(origin.matches(URL(string: "http://h.test:9/api/jobs")!))
        XCTAssertTrue(origin.matches(URL(string: "HTTP://H.TEST:9/")!))
        XCTAssertFalse(origin.matches(URL(string: "http://h.test:10/")!))
        XCTAssertFalse(origin.matches(URL(string: "http://other.test:9/")!))
        XCTAssertFalse(origin.matches(URL(string: "https://h.test:9/")!))
        XCTAssertFalse(origin.matches(URL(string: "file:///etc/passwd")!))
    }
}
