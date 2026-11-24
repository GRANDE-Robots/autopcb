"""build_desktop.py: build the paired CLI/desktop bundle and a platform installer on the host OS.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


class WindowsInstaller:
    """Builds the Windows installer with Inno Setup."""

    @staticmethod
    def compiler(requested: Path | None) -> Path:
        """Locate Inno Setup's compiler.

        Inputs:
            requested: A path to use as given, or None to search PATH and the standard install folders.
        Outputs:
            The compiler path. Raises ValueError when it does not exist or Inno Setup 6 is not installed.
        """
        if requested is not None:
            candidate = requested.expanduser().resolve()
            if not candidate.is_file():
                raise ValueError(f"Inno Setup compiler does not exist: {candidate}")
            return candidate
        if located := shutil.which("ISCC"):
            return Path(located).resolve()
        roots = [
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
            Path(os.environ.get("ProgramFiles", "C:/Program Files")),
        ]
        if local := os.environ.get("LOCALAPPDATA"):
            roots.append(Path(local) / "Programs")
        for root in roots:
            candidate = root / "Inno Setup 6" / "ISCC.exe"
            if candidate.is_file():
                return candidate.resolve()
        raise ValueError("Install Inno Setup 6 or pass --iscc PATH to its compiler.")

    @staticmethod
    def check_payload(executable: Path, *, require_x64: bool) -> None:
        # Inspect the built file without launching it on the compiler host.
        """Check the built executable is a Windows program, and 64-bit when required, without running it.

        Raises RuntimeError when the file is missing or is not a valid x64 PE executable.
        """
        if not executable.is_file():
            raise RuntimeError(f"PyInstaller did not produce {executable}")
        with executable.open("rb") as binary:
            dos = binary.read(64)
            if len(dos) != 64 or dos[:2] != b"MZ":
                raise RuntimeError(f"Invalid Windows executable: {executable}")
            binary.seek(struct.unpack_from("<I", dos, 60)[0])
            pe = binary.read(6)
        if len(pe) != 6 or pe[:4] != b"PE\0\0":
            raise RuntimeError(f"Invalid Windows executable: {executable}")
        if require_x64 and pe[4:] != b"\x64\x86":
            raise RuntimeError(
                "Build the Windows installer with an x64 Python runtime."
            )

    @staticmethod
    def build(bundle: Path, output: Path, version: str, compiler: Path) -> Path:
        """Write the Inno Setup script for a bundle and compile it.

        Inputs:
            bundle: The PyInstaller output folder.
            output: Folder that receives the installer.
            version: Project version.
            compiler: Path of Inno Setup's compiler.
        Outputs:
            The installer path, ``AutoPCB-<version>-windows.exe`` in ``output``.
        """
        spec = bundle.parent / "AutoPCB.iss"
        spec.write_text(
            f"""[Setup]
AppId=AutoPCB
AppName=AutoPCB
AppVersion={version}
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
DefaultDirName={{localappdata}}\\Programs\\AutoPCB
PrivilegesRequired=lowest
OutputDir={output}
OutputBaseFilename=AutoPCB-{version}-windows
Compression=lzma
SolidCompression=yes
[Files]
Source: "{bundle}\\*"; DestDir: "{{app}}"; Flags: recursesubdirs createallsubdirs ignoreversion
[Icons]
Name: "{{userprograms}}\\AutoPCB"; Filename: "{{app}}\\AutoPCB.exe"; Parameters: "desktop"; WorkingDir: "{{app}}"
""",
            encoding="utf-8-sig",
        )
        InstallerBuilder.run(compiler, spec)
        return output / f"AutoPCB-{version}-windows.exe"


class InstallerBuilder:
    """Runs the packaging tools and builds the macOS disk image and the Debian package."""

    @staticmethod
    def run(*arguments: str | Path) -> None:
        """Run one packaging command in the project root; raises CalledProcessError on failure."""
        subprocess.run([str(arg) for arg in arguments], cwd=ROOT, check=True)

    @staticmethod
    def macos(bundle: Path, output: Path, version: str) -> Path:
        """Build a macOS disk image holding the app bundle.

        Inputs:
            bundle: The PyInstaller output folder.
            output: Folder that receives the image.
            version: Project version.
        Outputs:
            The ``AutoPCB-<version>-macos.dmg`` path. The app is signed first when
            ``AUTOPCB_CODESIGN_IDENTITY`` names an identity.
        """
        image = bundle.parent / "image"
        contents = image / "AutoPCB.app" / "Contents"
        mac = contents / "MacOS"
        mac.mkdir(parents=True)
        engine = contents / "Resources" / "AutoPCB"
        shutil.copytree(bundle, engine, symlinks=True)
        start = mac / "launch"
        start.write_text(
            '#!/bin/sh\nexec "$(dirname "$0")/../Resources/AutoPCB/AutoPCB" desktop "$@"\n',
            encoding="utf-8",
        )
        start.chmod(0o755)
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleName": "AutoPCB",
                    "CFBundleIdentifier": "org.granderobots.autopcb",
                    "CFBundleExecutable": "launch",
                    "CFBundlePackageType": "APPL",
                    "CFBundleShortVersionString": version.partition(".dev")[0],
                    "NSHighResolutionCapable": True,
                },
                handle,
            )
        if signer := os.environ.get("AUTOPCB_CODESIGN_IDENTITY"):
            InstallerBuilder.run(
                "codesign",
                "--force",
                "--deep",
                "--timestamp",
                "--options",
                "runtime",
                "--sign",
                signer,
                contents.parent,
            )
        target = output / f"AutoPCB-{version}-macos.dmg"
        InstallerBuilder.run(
            "hdiutil",
            "create",
            "-volname",
            "AutoPCB",
            "-srcfolder",
            image,
            "-fs",
            "HFS+",
            target,
        )
        return target

    @staticmethod
    def linux(bundle: Path, output: Path, version: str) -> Path:
        """Build a Debian package that installs the bundle under ``/opt/autopcb``.

        Inputs:
            bundle: The PyInstaller output folder.
            output: Folder that receives the package.
            version: Project version.
        Outputs:
            The ``autopcb_<version>_<architecture>.deb`` path. It depends on KiCad 10 and installs a
            ``/usr/bin/autopcb`` link, a desktop entry and its icon.
        """
        package = bundle.parent / "package"
        control = package / "DEBIAN"
        control.mkdir(parents=True)
        arch = subprocess.check_output(
            ["dpkg", "--print-architecture"], text=True
        ).strip()
        (control / "control").write_text(
            f"Package: autopcb\nVersion: {version}\nSection: electronics\nPriority: optional\n"
            f"Architecture: {arch}\nMaintainer: AutoPCB\nDepends: kicad (>= 10.0)\n"
            "Description: Local KiCad project design and verification application\n",
            encoding="utf-8",
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
            "Icon=autopcb\nCategories=Development;Electronics;\nTerminal=false\n",
            encoding="utf-8",
        )
        icons = package / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"
        icons.mkdir(parents=True)
        shutil.copy2(ROOT / "packaging" / "autopcb.svg", icons / "autopcb.svg")
        result = output / f"autopcb_{version}_{arch}.deb"
        InstallerBuilder.run(
            "dpkg-deb", "--root-owner-group", "--build", package, result
        )
        return result


class DesktopBuildCommand:
    """Command-line entry: build the desktop bundle and, unless ``--bundle-only``, its installer."""

    def __call__(self) -> int:
        """Build the bundle with PyInstaller and, unless ``--bundle-only``, the platform installer.

        Reads ``--output``, a new directory, ``--bundle-only`` and, on Windows, ``--iscc``.
        Prints the artifact path and returns 0. Raises RuntimeError when the installer
        command produces no artifact and FileExistsError if the output appears during the build.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--output", type=Path, required=True, help="new output directory"
        )
        parser.add_argument("--bundle-only", action="store_true")
        parser.add_argument(
            "--iscc", type=Path, help="path to Inno Setup's Windows compiler"
        )
        args = parser.parse_args()
        on_windows = sys.platform == "win32"
        if args.iscc is not None and (not on_windows or args.bundle_only):
            parser.error("--iscc applies only to a Windows installer build")
        output = args.output.resolve()
        if output.exists() or not output.parent.is_dir():
            parser.error("--output must be a new directory under an existing parent")
        if on_windows and not args.bundle_only:
            try:
                compiler = WindowsInstaller.compiler(args.iscc)
            except ValueError as error:
                parser.error(str(error))
        import tomllib

        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["version"]
        with tempfile.TemporaryDirectory(
            prefix="autopcb-package-", dir=output.parent
        ) as temporary:
            scratch = Path(temporary)
            engine = scratch / "engine"
            shutil.copytree(
                ROOT / "scripts",
                engine,
                ignore=shutil.ignore_patterns("__pycache__", "desktop.ini", "*.pyc"),
            )
            InstallerBuilder.run(
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--onedir",
                "--name",
                "AutoPCB",
                "--distpath",
                scratch / "dist",
                "--workpath",
                scratch / "work",
                "--specpath",
                scratch,
                "--paths",
                ROOT / "scripts",
                "--add-data",
                str(ROOT / "scripts/autopcb/catalog.json") + os.pathsep + "autopcb",
                "--add-data",
                str(ROOT / "scripts/autopcb/serve.html") + os.pathsep + "autopcb",
                "--add-data",
                str(engine) + os.pathsep + "engine",
                ROOT / "scripts/autopcb/__main__.py",
            )
            bundle = scratch / "dist" / "AutoPCB"
            executable = bundle / ("AutoPCB.exe" if on_windows else "AutoPCB")
            if on_windows:
                WindowsInstaller.check_payload(
                    executable, require_x64=not args.bundle_only
                )
            else:
                InstallerBuilder.run(executable, "--help")
            stage = scratch / "ready"
            stage.mkdir()
            if args.bundle_only:
                shutil.move(bundle, stage / bundle.name)
                artifact = stage / bundle.name
            elif on_windows:
                artifact = WindowsInstaller.build(bundle, stage, version, compiler)
            else:
                builder = {
                    "darwin": InstallerBuilder.macos,
                    "linux": InstallerBuilder.linux,
                }.get(sys.platform)
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


main = DesktopBuildCommand()

if __name__ == "__main__":
    raise SystemExit(main())
