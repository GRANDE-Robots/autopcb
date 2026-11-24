# workflows

GitHub Actions for AutoPCB. Pull requests run the test workflow on Linux, macOS and Windows. Installers are built only when someone starts the installers workflow by hand.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`installers.yml`](installers.yml) | Manual workflow that builds the Windows installer on a `windows-2022` runner and uploads it as the `autopcb-windows-installer` artifact. The SHA-256 is printed in the build log. | `packaging/build_desktop.py`, the `desktop-build` extra in `pyproject.toml`, Inno Setup 6 | Started by hand from the Actions tab |
| [`tests.yml`](tests.yml) | Installs KiCad 10 and runs `scripts/run.py test -v` on `ubuntu-24.04`, `macos-15` and `windows-2022`. Runs for pull requests that change `scripts/`, `config/`, `pyproject.toml` or the workflows, and on manual start. | `scripts/run.py`, KiCad 10 from the KiCad PPA, Homebrew and Chocolatey, `psutil`, `markdown-it-py`, `Pillow`, `pypdfium2`, `ruff` | GitHub pull requests |
