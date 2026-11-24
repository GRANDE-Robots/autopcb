#!/usr/bin/env bash
# check_linux_release.sh: check the AutoPCB Debian package on an Ubuntu system with KiCad 10.
#
# Usage: check_linux_release.sh PHASE PACKAGE.deb EVIDENCE_DIR
#   install  Install the package, check its files and desktop entry, run the CLI.
#   qualify  Run both reference projects with the installed program and KiCad.
#   remove   Remove the package and check that nothing it installed is left.
# Run the phases in this order with the same package and evidence directory. The
# install phase needs a new evidence directory. Packages that need root use sudo
# when the user is not root. Set CHECK_KICAD_PYTHON when the system python3 is not the
# interpreter that imports KiCad's pcbnew. The desktop launch stays a manual check.
#
# Copyright: GRANDE Robots and Aaron John Sabu
set -euo pipefail

phase=${1:?usage: check_linux_release.sh install|qualify|remove PACKAGE.deb EVIDENCE_DIR}
package=$(realpath "${2:?missing PACKAGE.deb}")
evidence=$(realpath -m "${3:?missing EVIDENCE_DIR}")
report="$evidence/linux-qualification.json"
workspace="$evidence/CLI Workspace"
launcher=/usr/bin/autopcb
program=/opt/autopcb/AutoPCB
entry=/usr/share/applications/autopcb.desktop
icon=/usr/share/icons/hicolor/scalable/apps/autopcb.svg
sudo_cmd=()
if [ "$(id -u)" -ne 0 ]; then sudo_cmd=(sudo); fi

fail() { echo "FAIL: $*" >&2; exit 1; }
check() { local message=$1; shift; "$@" || fail "$message"; }

# Run the installed program the way a user would, without inherited tool settings.
# CHECK_KICAD_PYTHON names KiCad's interpreter on a system whose python3 is another one.
autopcb() {
    local native=()
    if [ -n "${CHECK_KICAD_PYTHON:-}" ]; then native=(--kicad-python "$CHECK_KICAD_PYTHON"); fi
    env -u PYTHONHOME -u PYTHONPATH -u QT_PLUGIN_PATH -u QT_QPA_PLATFORM \
        -u KICAD_CLI -u KICAD_PYTHON "$launcher" --workspace "$workspace" "${native[@]}" "$@"
}

record() {
    python3 - "$report" "$@" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text()) if path.exists() else {"schema_version": 1}
for item in sys.argv[2:]:
    key, _, value = item.partition("=")
    data[key] = value
path.write_text(json.dumps(data, indent=2) + "\n")
PY
}

case "$phase" in
install)
    [ ! -e "$evidence" ] || fail "Choose a new evidence directory: $evidence."
    check "The package file is missing: $package." test -f "$package"
    check "Run this check on a Debian or Ubuntu system." command -v dpkg-deb >/dev/null
    ! dpkg -s autopcb >/dev/null 2>&1 || fail "AutoPCB is already installed on this system."
    mkdir -p "$workspace"
    record "phase=install" "package=$package" \
        "package_sha256=$(sha256sum "$package" | cut -d' ' -f1)" \
        "architecture=$(dpkg --print-architecture)" "os=$(. /etc/os-release && echo "$PRETTY_NAME")"
    dpkg-deb --field "$package" Depends | grep -q 'kicad' || fail "The package does not depend on KiCad."
    "${sudo_cmd[@]}" apt-get install -y "$package"
    check "The package is not installed." dpkg -s autopcb
    check "$launcher is not a link to $program." test "$(readlink "$launcher")" = "$program"
    check "$program is not executable." test -x "$program"
    check "The desktop entry is missing." grep -q '^Exec=/usr/bin/autopcb desktop$' "$entry"
    check "The desktop entry has no icon name." grep -q '^Icon=autopcb$' "$entry"
    check "The icon is missing." test -s "$icon"
    if command -v desktop-file-validate >/dev/null; then
        check "The desktop entry is invalid." desktop-file-validate "$entry"
    fi
    help_text=$(autopcb --help) || fail "The installed CLI could not show help."
    grep -q qualify <<<"$help_text" || fail "The installed help does not list qualify."
    autopcb doctor > "$evidence/doctor.json" || fail "doctor failed. See $evidence/doctor.json."
    python3 - "$evidence/doctor.json" <<'PY' || fail "doctor did not find a KiCad 10 toolchain."
import json, sys
report = json.load(open(sys.argv[1]))
assert report["status"] == "ok" and report["kicad_version"].startswith("10."), report
PY
    record "installed_program_sha256=$(sha256sum "$program" | cut -d' ' -f1)" "status=awaiting_qualify"
    echo "Install passed. Run the qualify phase next."
    ;;
qualify)
    check "Run the install phase first." grep -q awaiting_qualify "$report"
    check "The installed program changed since the install phase." \
        grep -q "$(sha256sum "$program" | cut -d' ' -f1)" "$report"
    qualification="$evidence/reference-projects"
    set +e
    autopcb qualify --output "$qualification" --rounds 45 --seconds 8 --memory-gib 2 \
        > "$evidence/qualify.stdout.txt" 2> "$evidence/qualify.stderr.txt"
    code=$?
    set -e
    python3 - "$qualification" "$code" <<'PY' || fail "Unexpected reference qualification outcome."
import hashlib, json, pathlib, sys

root, code = pathlib.Path(sys.argv[1]), int(sys.argv[2])
result = json.loads((root / "qualification.json").read_text())
names = sorted(case["name"] for case in result["cases"])
assert code == 2 and result["status"] == "qualification_incomplete", (code, result["status"])
assert names == ["Temperature Sensor", "USB Breakout"], names
for case in result["cases"]:
    applied = case.get("applied") or {}
    assert case["stage"] == "done" and case["native_passed"] and case["saved_opens"] == 0, case["name"]
    assert applied.get("only_pcb_changed") and applied.get("locked_editor_rejected"), case["name"]
    assert applied.get("stale_rules_rejected"), case["name"]
    assert case["checked_export"] == "blocked", case["name"]
    assert case["blockers"] == ["A source-bound electrical review report is required."], case["name"]
manifests = sorted((root / "results" / "exports").rglob("manifest.json"))
assert len(manifests) == 2, manifests
required = {"bom.csv", "placements.csv", "schematic.pdf", "assembly.pdf",
            "evidence/erc.json", "evidence/drc.json", "evidence/reference.json"}
for path in manifests:
    manifest = json.loads(path.read_text())
    assert manifest["status"] == "draft" and manifest["production_approved"] is False, path
    artifacts = manifest["artifacts"]
    assert required <= set(artifacts), (path, required - set(artifacts))
    assert any(name.startswith("gerbers/") and name.endswith(".gbrjob") for name in artifacts), path
    assert any(name.startswith("drills/") and name.endswith(".drl") for name in artifacts), path
    for name, digest in artifacts.items():
        assert hashlib.sha256((path.parent / name).read_bytes()).hexdigest().upper() == digest.upper(), name
PY
    record "reference_qualification=$qualification/qualification.json" "status=awaiting_remove"
    echo "Qualify passed. Run the remove phase next."
    ;;
remove)
    check "Run the qualify phase first." grep -q awaiting_remove "$report"
    "${sudo_cmd[@]}" apt-get remove -y autopcb
    ! dpkg -s autopcb >/dev/null 2>&1 || fail "The package is still installed."
    for left in "$launcher" "$program" "$entry" "$icon"; do
        [ ! -e "$left" ] && [ ! -L "$left" ] || fail "$left is still present."
    done
    check "The evidence directory was removed." test -f "$report"
    record "status=passed"
    echo "Remove passed. The Linux package check is complete."
    ;;
*)
    fail "Unknown phase $phase. Use install, qualify or remove."
    ;;
esac
