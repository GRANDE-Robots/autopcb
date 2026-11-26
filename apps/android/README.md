# AutoPCB for Android

## Overview

A phone cannot run KiCad, so this app is a thin shell that controls `autopcb serve` running on your computer. It pairs with the server once, keeps the pairing in encrypted storage, and shows the server's web page in a WebView that cannot leave the paired address. Plain Android Views, Kotlin, no Jetpack Compose. Application id `com.grande.autopcb`, minSdk 26, compileSdk and targetSdk 35, Java 17.

## Pairing

1. On the computer run `autopcb serve --host ADDRESS`, where `ADDRESS` is the LAN or Tailscale address the phone can reach. The server prints a link `http://HOST:PORT/#token=TOKEN` and draws it as a QR code.
2. In the app tap Scan QR code, or paste the link into the text field and tap Pair.
3. The app calls `/api/info` with the token. Only an AutoPCB API level 1 server that accepts the token is stored; every other answer shows a message (cannot reach the computer, not a supported AutoPCB server, token rejected).

The main screen checks the server whenever the app comes to the front. A lost connection or a rejected token covers the page with a message, Retry (lost connection only) and Pair again. The menu item Forget this computer deletes the pairing and returns to the pair screen.

## Build and test

Needs JDK 17 or newer and the Android SDK (`ANDROID_HOME`, platform `android-35`, build-tools `35.0.0`). Run in this folder:

```
./gradlew assembleDebug testDebugUnitTest
./gradlew assembleDebugAndroidTest
./gradlew connectedDebugAndroidTest
```

The first two build the debug APK (`app/build/outputs/apk/debug/app-debug.apk`), run the JVM unit tests (link parser, origin check, `/api/info` classifier) and compile the instrumented test. The third runs the Espresso pair-flow test against an in-process fake server on 127.0.0.1 and needs an emulator or device. It runs only in CI (`.github/workflows/android.yml`, API 34 emulator); it has not been run on a developer machine.

## Security notes

- The token and origin are stored with `EncryptedSharedPreferences` under a Keystore-backed master key. The token is never logged, never written to plain preferences, and `android:allowBackup` is off.
- The WebView loads `{origin}/#token=TOKEN`; the token travels in the URL fragment, so it never reaches a server log, and the page moves it into its own session storage.
- The WebView refuses, without opening a browser, every navigation whose scheme, host or port differs from the paired origin. File and content access and remote debugging are off.
- Cleartext limitation: `autopcb serve` speaks plain HTTP, so the app allows cleartext traffic through its network security config. The token and everything the page shows cross the network unencrypted. Use the link only over a network you trust, such as Tailscale or an isolated LAN.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`.gitignore`](.gitignore) | Keeps Gradle caches, build output and `local.properties` out of git. | None | git |
| [`README.md`](README.md) | This guide. | None | Readers, `../../README.md` |
| [`app`](app) | The application module: sources, resources, manifest, unit and instrumented tests. | `build.gradle.kts`, Android SDK 35 | `settings.gradle.kts` |
| [`build.gradle.kts`](build.gradle.kts) | Pins the Android Gradle Plugin and Kotlin versions. | Gradle | `app` |
| [`gradle`](gradle) | The Gradle wrapper files that fix the Gradle version. | Network on first use | `gradlew`, `gradlew.bat` |
| [`gradle.properties`](gradle.properties) | Gradle memory, AndroidX and Kotlin style switches. | None | Gradle |
| [`gradlew`](gradlew) | Wrapper script for Linux and macOS. | `gradle`, JDK 17 or newer | Developers, `../../.github/workflows/android.yml` |
| [`gradlew.bat`](gradlew.bat) | Wrapper script for Windows. | `gradle`, JDK 17 or newer | Developers |
| [`settings.gradle.kts`](settings.gradle.kts) | Names the project, includes `app` and sets the repositories. | None | Gradle |
