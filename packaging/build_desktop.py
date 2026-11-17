"""Build the paired CLI/desktop bundle and a platform installer on the host OS."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(*arguments: str | Path) -> None:
    subprocess.run([str(arg) for arg in arguments], cwd=ROOT, check=True)


def windows(bundle: Path, output: Path, version: str) -> Path:
    candidate = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Inno Setup 6/ISCC.exe"
    compiler = shutil.which("ISCC") or (str(candidate) if candidate.is_file() else None)
    if not compiler:
        raise RuntimeError("Install Inno Setup 6 or add ISCC to PATH to build the Windows installer.")
    spec = bundle.parent / "AutoPCB.iss"
    spec.write_text(
        f"""[Setup]
AppName=AutoPCB
AppVersion={version}
DefaultDirName={{localappdata}}\\Programs\\AutoPCB
PrivilegesRequired=lowest
OutputDir={output}
OutputBaseFilename=AutoPCB-{version}-windows
Compression=lzma
SolidCompression=yes
[Files]
Source: "{bundle}\\*"; DestDir: "{{app}}"; Flags: recursesubdirs createallsubdirs ignoreversion
[Icons]
Name: "{{userprograms}}\\AutoPCB"; Filename: "{{app}}\\AutoPCB.exe"; Parameters: "desktop"
""", encoding="utf-8",
    )
    run(compiler, spec)
    return output / f"AutoPCB-{version}-windows.exe"


def macos(bundle: Path, output: Path, version: str) -> Path:
    image = bundle.parent / "image"
    contents = image / "AutoPCB.app" / "Contents"
    mac = contents / "MacOS"
    mac.mkdir(parents=True)
    engine = contents / "Resources" / "AutoPCB"
    shutil.copytree(bundle, engine, symlinks=True)
    start = mac / "launch"
    start.write_text('#!/bin/sh\nexec "$(dirname "$0")/../Resources/AutoPCB/AutoPCB" desktop "$@"\n', encoding="utf-8")
    start.chmod(0o755)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleName": "AutoPCB", "CFBundleIdentifier": "org.granderobots.autopcb",
                       "CFBundleExecutable": "launch", "CFBundlePackageType": "APPL",
                       "CFBundleShortVersionString": version.partition(".dev")[0], "NSHighResolutionCapable": True}, handle)
    if signer := os.environ.get("AUTOPCB_CODESIGN_IDENTITY"):
        run("codesign", "--force", "--deep", "--timestamp", "--options", "runtime",
            "--sign", signer, contents.parent)
    target = output / f"AutoPCB-{version}-macos.dmg"
    run("hdiutil", "create", "-volname", "AutoPCB", "-srcfolder", image, "-fs", "HFS+", target)
    return target


def linux(bundle: Path, output: Path, version: str) -> Path:
    package = bundle.parent / "package"
    control = package / "DEBIAN"
    control.mkdir(parents=True)
    arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    (control / "control").write_text(
        f"Package: autopcb\nVersion: {version}\nSection: electronics\nPriority: optional\n"
        f"Architecture: {arch}\nMaintainer: AutoPCB\n"
        "Description: Local KiCad project design and verification application\n", encoding="utf-8",
    )
    engine = package / "opt" / "autopcb"
    shutil.copytree(bundle, engine, symlinks=True)
    binaries = package / "usr" / "bin"
    binaries.mkdir(parents=True)
    (binaries / "autopcb").symlink_to("/opt/autopcb/AutoPCB")
    shortcuts = package / "usr" / "share" / "applications"
    shortcuts.mkdir(parents=True)
    (shortcuts / "autopcb.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=AutoPCB\nExec=/usr/bin/autopcb desktop\n"
        "Categories=Development;Electronics;\nTerminal=false\n", encoding="utf-8",
    )
    result = output / f"autopcb_{version}_{arch}.deb"
    run("dpkg-deb", "--root-owner-group", "--build", package, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new output directory")
    parser.add_argument("--bundle-only", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or not output.parent.is_dir():
        parser.error("--output must be a new directory under an existing parent")
    import tomllib

    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    with tempfile.TemporaryDirectory(prefix="autopcb-package-", dir=output.parent) as temporary:
        scratch = Path(temporary)
        engine = scratch / "engine"
        shutil.copytree(ROOT / "scripts", engine, ignore=shutil.ignore_patterns("__pycache__", "desktop.ini", "*.pyc"))
        run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--name", "AutoPCB",
            "--distpath", scratch / "dist", "--workpath", scratch / "work", "--specpath", scratch,
            "--paths", ROOT / "scripts", "--add-data", str(ROOT / "scripts/autopcb/catalog.json") + os.pathsep + "autopcb", "--add-data", str(engine) + os.pathsep + "engine",
            ROOT / "scripts/autopcb/__main__.py")
        bundle = scratch / "dist" / "AutoPCB"
        executable = bundle / ("AutoPCB.exe" if sys.platform == "win32" else "AutoPCB")
        run(executable, "--help")
        stage = scratch / "ready"
        stage.mkdir()
        if args.bundle_only:
            shutil.move(bundle, stage / bundle.name)
            artifact = stage / bundle.name
        else:
            builder = {"win32": windows, "darwin": macos, "linux": linux}.get(sys.platform)
            if not builder:
                parser.error(f"Unknown installer platform: {sys.platform}")
            artifact = builder(bundle, stage, version)
        if not artifact.exists():
            raise RuntimeError("Installer command returned without an artifact")
        if output.exists():
            raise FileExistsError(f"Output appeared during the build: {output}")
        stage.rename(output)
    print(output / artifact.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
