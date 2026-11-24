# packaging

Builds and checks the desktop distribution of AutoPCB.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`build_desktop.py`](build_desktop.py) | Builds the paired command-line and desktop bundle with PyInstaller, then a platform installer on the host system: Inno Setup on Windows, a disk image on macOS and a Debian package on Linux. | PyInstaller, PySide6, Inno Setup 6 on Windows | `.github/workflows/installers.yml`, manual builds |
| [`check_windows_release.ps1`](check_windows_release.ps1) | Two-phase check of the Windows installer on a clean Windows 11 x64 VM. Phase Install checks setup, the Start menu entry and the missing-KiCad diagnostic. Phase Qualify, after installing KiCad 10.0.6, runs both reference projects from the installed executable and checks uninstall. | A built installer, KiCad 10.0.6 | Manual qualification on a VM |
