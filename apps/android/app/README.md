# app

The Android application module: pairing, encrypted storage, the locked WebView and their tests.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`build.gradle.kts`](build.gradle.kts) | Android and Kotlin build settings, SDK levels and dependencies. | `../build.gradle.kts` | Gradle |
| [`src/main`](src/main) | App sources under `kotlin/com/grande/autopcb`, resources, manifest. | AppCompat, security-crypto, zxing-android-embedded | `build.gradle.kts` |
| [`src/test`](src/test) | JVM unit tests: link parser, origin check, `/api/info` classifier. | JUnit, `org.json` | `testDebugUnitTest` |
| [`src/androidTest`](src/androidTest) | Espresso pair-flow test and its in-process fake server; runs in CI only. | AndroidX Test, Espresso | `connectedDebugAndroidTest` |
