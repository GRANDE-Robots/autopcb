# apps

Native phone shells for AutoPCB. A phone cannot run KiCad, so each app pairs with `autopcb serve` on a computer that has it and shows that server's page in a view locked to the paired address. Pair by scanning the QR code that `autopcb serve` draws, or by pasting its link.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`android/`](android/README.md) | The Android app in Kotlin and Gradle: pairing screen, token in encrypted storage, and a locked WebView with lost-connection and rejected-token states. | Android SDK 35, Gradle, `scripts/autopcb/serve.py` | `.github/workflows/android.yml` |
| [`ios/`](ios/README.md) | The iOS app in Swift and SwiftUI: the same flow with the token in the Keychain. Verified only in the CI simulator, because installing on a real iPhone needs an Apple Developer account. | Xcode 16, XcodeGen, `scripts/autopcb/serve.py` | `.github/workflows/ios.yml` |
