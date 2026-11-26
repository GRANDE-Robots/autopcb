# workflows

GitHub Actions for AutoPCB. Pull requests run the test workflow on Linux, macOS and Windows. Installers are built only when someone starts the installers workflow by hand. The phone app workflows run for pull requests that change `apps/`.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`android.yml`](android.yml) | Builds the Android debug APK and runs the unit tests on `ubuntu-24.04`, uploads the APK with a SHA-256 file, then runs the Espresso pair-flow test on an API 34 emulator. Runs for pull requests that change `apps/android/` and on manual start. | JDK 17, Android SDK, `reactivecircus/android-emulator-runner` | GitHub pull requests |
| [`installers.yml`](installers.yml) | Manual workflow with `windows`, `linux` and `macos` jobs. It builds the Windows installer, the Debian package and the macOS disk image from the selected commit and uploads each with a SHA-256 file. The Linux job also installs the package and runs the three phases of `packaging/check_linux_release.sh`. | `packaging/build_desktop.py`, `packaging/check_linux_release.sh`, the `desktop-build` extra in `pyproject.toml`, Inno Setup 6, KiCad 10 from the KiCad PPA | Started by hand from the Actions tab |
| [`ios.yml`](ios.yml) | Generates the Xcode project with XcodeGen and runs the unit and UI tests on an iPhone simulator on `macos-15`. Runs for pull requests that change `apps/ios/` and on manual start. | Xcode 16, XcodeGen | GitHub pull requests |
| [`tests.yml`](tests.yml) | Installs KiCad 10 and runs `scripts/run.py test -v` on `ubuntu-24.04`, `macos-15` and `windows-2022`. Runs for pull requests that change `scripts/`, `config/`, `pyproject.toml` or the workflows, and on manual start. | `scripts/run.py`, KiCad 10 from the KiCad PPA, Homebrew and Chocolatey, `psutil`, `markdown-it-py`, `Pillow`, `pypdfium2`, `ruff` | GitHub pull requests |
