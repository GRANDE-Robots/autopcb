# AutoPCB for iOS

A thin SwiftUI shell that controls `autopcb serve` running on your computer. A phone cannot run KiCad, so the app pairs with the server, keeps the pairing in the Keychain, and shows the server's web page in a locked web view.

**Build status: not compiled by the author.** The code was written in a container with no Xcode. The pure logic (`Core/`) and its unit tests were compiled and run with the Swift 6.0.3 toolchain on Linux. The SwiftUI, UIKit, AVFoundation, WebKit and Security code and the UI tests were not compiled anywhere. The first real build is the macOS CI job.

## How to pair

1. On the computer, run `autopcb serve --host ADDRESS`, where ADDRESS is a LAN or Tailscale address of the computer.
2. The server prints a link of the form `http://HOST:PORT/#token=TOKEN` and draws it as a QR code.
3. In the app, tap Scan QR code and point the camera at the code, or paste the link into the field and tap Pair.
4. The app calls `/api/info` with the token. Only a server that is AutoPCB, speaks API level 1 and accepts the token is stored.

Messages: unreachable (any network error or a 5 second timeout), not an AutoPCB server or unsupported API level, token rejected, or ready.

## Build and test

Requires macOS with Xcode 16 and [XcodeGen](https://github.com/yonaskolb/XcodeGen).

```sh
brew install xcodegen
cd apps/ios
xcodegen generate
xcodebuild test -project AutoPCB.xcodeproj -scheme AutoPCB \
  -destination 'platform=iOS Simulator,name=iPhone 16'
```

The Xcode project is generated from `project.yml` and is not committed. The same steps run in `.github/workflows/ios.yml`, which picks the first available iPhone simulator.

Unit tests cover the link parser and the `/api/info` classifier. The UI tests check that a fresh launch shows the Scan, link and Pair controls and that `http://127.0.0.1:1/#token=x` shows the unreachable message. They assume no stored pairing, so reset the simulator if a manual run paired it.

## Security notes

- The token and origin are stored as one Keychain item (`kSecClassGenericPassword`, `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`). They are never written to UserDefaults or logged. The token is sent only in the `Authorization` header and in the URL fragment, which never reaches a server log.
- The web view is locked to the paired origin. Any navigation whose scheme, host or port differs is cancelled without a message, which also blocks `file:` URLs. The web view uses a non-persistent data store.
- Forget this computer, and Pair again, delete the Keychain item and return to the pair screen.
- The server speaks plain HTTP. `Info.plist` sets `NSAllowsArbitraryLoads` to true because the exception for local networks does not cover Tailscale addresses (100.x.y.z or MagicDNS names), which iOS does not treat as local. Anyone who can see the traffic can read the token, so use the link only over a network you trust: a home LAN you control or Tailscale, which encrypts traffic between your devices.
- `NSLocalNetworkUsageDescription` explains the local network permission prompt; `NSCameraUsageDescription` explains the camera prompt.

## Limits

The app is verified only by the CI simulator run; there is no hardware evidence. The simulator has no camera, so QR scanning is untested until a real device is used. Installing on a real iPhone needs an Apple Developer account to sign the app.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`Core/`](Core/) | Pure Swift logic with no UI or I/O: the pairing link parser, origin check and `/api/info` classifier. | Foundation | `Sources`, `Tests` |
| [`Resources/`](Resources/) | `Info.plist` and the asset catalog with the 1024 px app icon. | `scripts/make_icon.py` for the icon | `project.yml` |
| [`Sources/`](Sources/) | The app: entry point, screens, Keychain store, server probe, web view and QR scanner. | `Core`, SwiftUI, WebKit, AVFoundation, Security | `project.yml` |
| [`Tests/`](Tests/) | XCTest unit tests for `Core`. | `Core`, XCTest | `project.yml` |
| [`UITests/`](UITests/) | XCUITest checks of the pair screen. | The `AutoPCB` app, XCTest | `project.yml` |
| [`scripts/`](scripts/) | `make_icon.py`, which redraws the app icon with Pillow: `python3 scripts/make_icon.py Resources/Assets.xcassets/AppIcon.appiconset/AppIcon.png`. | Pillow | Maintainers |
| [`project.yml`](project.yml) | XcodeGen definition of the app, unit-test and UI-test targets; `Core` is compiled into the app and the unit tests. | XcodeGen | `.github/workflows/ios.yml` |
| [`README.md`](README.md) | This guide. | None | Maintainers |
