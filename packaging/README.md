# packaging

Builds and checks the desktop distribution of AutoPCB.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`autopcb.svg`](autopcb.svg) | The application icon: a gold octilinear trace with three pads on a green board. | None | `build_desktop.py` installs it in the Debian package |
| [`build_desktop.py`](build_desktop.py) | Builds the paired command-line and desktop bundle with PyInstaller, then a platform installer on the host system: Inno Setup on Windows, a disk image on macOS and a Debian package on Linux. The Debian package depends on KiCad 10. | PyInstaller, PySide6, Inno Setup 6 on Windows | `.github/workflows/installers.yml`, manual builds, `scripts/verification/test_build_desktop.py` |
| [`check_linux_release.sh`](check_linux_release.sh) | Three-phase check of the Debian package on an Ubuntu system with KiCad 10: `install` checks the files, desktop entry and `doctor`, `qualify` runs both reference projects, `remove` checks that nothing is left. | A built `.deb`, KiCad 10, `python3` | `.github/workflows/installers.yml`, manual qualification |
| [`check_windows_release.ps1`](check_windows_release.ps1) | Two-phase check of the Windows installer on a clean Windows 11 x64 VM. Phase Install checks setup, the Start menu entry and the missing-KiCad diagnostic. Phase Qualify, after installing KiCad 10.0.6, runs both reference projects from the installed executable and checks uninstall. | A built installer, KiCad 10.0.6 | Manual qualification on a VM |
