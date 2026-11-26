Installers for Windows, Linux and macOS, and a debug-signed Android APK. `SHA256SUMS` lists the checksum of every file.

**Install**

- Windows: run the `.exe`. It installs per user. Install KiCad 10 first.
- Linux: `sudo apt install ./AutoPCB-*.deb`.
- macOS: open the `.dmg` and drag AutoPCB to Applications.
- Android: install the APK, then pair it with `autopcb serve` on the computer. It is signed with a debug key, so it is for sideloading, not a store.

**What was checked**

- Software: the full test suite, the code-rules audit, `ruff format` and `ruff check` pass in CI on Linux, macOS and Windows.
- Linux package: installed, qualified on both reference designs and removed on a clean runner.
- Android: unit tests and an emulator pair-flow test in CI.
- iOS: simulator tests pass in CI. No iOS build is attached, because installing on an iPhone needs an Apple Developer account.

**Not checked**

- The Windows installer and the macOS disk image were built but not installed and qualified on a clean machine. The Windows check is manual: `packaging/check_windows_release.ps1`.
- The builds are not code-signed, so Windows and macOS will warn on first run.
- No hardware evidence. Bench bring-up of the reference boards is pending.
