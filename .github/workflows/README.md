# workflows

GitHub Actions for AutoPCB. Pull requests run the test workflow on Linux, macOS and Windows. Installers are built only when someone starts the installers workflow by hand.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`installers.yml`](installers.yml) | Manual workflow with `windows`, `linux` and `macos` jobs. It builds the Windows installer, the Debian package and the macOS disk image from the selected commit and uploads each with a SHA-256 file. The Linux job also installs the package and runs the three phases of `packaging/check_linux_release.sh`. | `packaging/build_desktop.py`, `packaging/check_linux_release.sh`, the `desktop-build` extra in `pyproject.toml`, Inno Setup 6, KiCad 10 from the KiCad PPA | Started by hand from the Actions tab |
| [`tests.yml`](tests.yml) | Installs KiCad 10 and runs `scripts/run.py test -v` on `ubuntu-24.04`, `macos-15` and `windows-2022`. Runs for pull requests that change `scripts/`, `config/`, `pyproject.toml` or the workflows, and on manual start. | `scripts/run.py`, KiCad 10 from the KiCad PPA, Homebrew and Chocolatey, `psutil`, `markdown-it-py`, `Pillow`, `pypdfium2`, `ruff` | GitHub pull requests |
