// PairScreenUITests.swift
// Responsibility: UI tests for a fresh launch: the pair screen controls and the
// unreachable message for a link that cannot connect.
//
// Copyright: GRANDE Robots and Aaron John Sabu

import XCTest

final class PairScreenUITests: XCTestCase {
    override func setUp() {
        continueAfterFailure = false
    }

    func testFreshLaunchShowsPairScreenControls() {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.buttons["scanButton"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.textFields["linkField"].exists)
        XCTAssertTrue(app.buttons["pairButton"].exists)
    }

    func testUnreachableLinkShowsUnreachableMessage() {
        let app = XCUIApplication()
        app.launch()
        let field = app.textFields["linkField"]
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        field.tap()
        field.typeText("http://127.0.0.1:1/#token=x")
        app.buttons["pairButton"].tap()
        let message = app.staticTexts["pairMessage"]
        XCTAssertTrue(message.waitForExistence(timeout: 15))
        XCTAssertTrue(message.label.contains("Cannot reach the computer"))
    }
}
