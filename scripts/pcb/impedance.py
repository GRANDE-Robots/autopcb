"""Screen routed straight differential pairs against reviewed, layer-specific geometry."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sys

import pcbnew

from core.compat import strict_zip
from core.kicad_sexpr import parse
from core.paths import data_path
from core.project import ProjectPaths, declared_pairs, read_profile
from pcb.net_routing.reference import high_speed_nets


@dataclass(frozen=True)
class Trace:
    x: float
    y: float
    dx: float
    dy: float
    length: float
    width: float

    @classmethod
    def from_item(cls, item):
        a, b = item.GetStart(), item.GetEnd()
        dx, dy = pcbnew.ToMM(b.x - a.x), pcbnew.ToMM(b.y - a.y)
        length = math.hypot(dx, dy)
        if length == 0:
            raise ValueError("A differential track has zero length")
        return cls(pcbnew.ToMM(a.x), pcbnew.ToMM(a.y), dx / length, dy / length,
                   length, pcbnew.ToMM(item.GetWidth()))

    def point(self, t, offset=0.0):
        return self.x + t * self.dx - offset * self.dy, self.y + t * self.dy + offset * self.dx

    def overlap(self, mate, gap, tolerance):
        if abs(self.dx * mate.dy - self.dy * mate.dx) > 1e-8:
            return None
        perpendicular = -(mate.x - self.x) * self.dy + (mate.y - self.y) * self.dx
        edge_gap = abs(perpendicular) - (self.width + mate.width) / 2
        if edge_gap < 0 or abs(edge_gap - gap) > tolerance + 1e-9:
            return None
        ends = [(x - self.x) * self.dx + (y - self.y) * self.dy
                for x, y in (mate.point(0), mate.point(mate.length))]
        lo, hi = max(0.0, min(ends)), min(self.length, max(ends))
        return (lo, hi, perpendicular) if hi > lo else None

    @staticmethod
    def union_length(intervals):
        total, end = 0.0, -math.inf
        for low, high in sorted(intervals):
            total += max(0.0, high - max(low, end))
            end = max(end, high)
        return total


class StackProfile:
    """Read geometry targets and verify their physical layer order against the board."""

    def __init__(self, data):
        self.data = data
        self.identifier = data["stackup_id"]
        self.physical = data["physical_stack_mm"]
        spec = data["controlled_impedance"]
        self.width_tol = self.number(spec["width_tolerance_mm"], zero=True)
        self.gap_tol = self.number(spec["gap_tolerance_mm"], zero=True)
        self.step = self.number(spec["sample_step_mm"])
        self.targets = {}
        self.ohms = {}
        copper = [r["layer"] for r in self.physical if r["type"].lower() == "copper"]
        if len(copper) < 2 or len(set(copper)) != len(copper) or len(copper) != data["copper_layers"]:
            raise ValueError("Invalid physical copper stack")
        for family in spec["families"]:
            cls = family["netclass"]
            if cls in self.ohms or not isinstance(cls, str) or not re.fullmatch(r"[\w .+-]+", cls):
                raise ValueError("Use a unique, literal KiCad net class for each impedance target")
            self.ohms[cls] = self.number(family["target_ohms"])
            for row in family["layers"]:
                width, gap = self.number(row["width_mm"]), self.number(row["gap_mm"])
                floor = row.get("connection_floor_mm")
                if floor is not None and self.number(floor) > width:
                    raise ValueError("The connection floor exceeds the target width")
                for layer, refs in strict_zip(row["signal_layers"], row["reference_layers"]):
                    pos = copper.index(layer)
                    neighbors = copper[max(0, pos - 1):pos] + copper[pos + 1:pos + 2]
                    if ((cls, layer) in self.targets or not isinstance(refs, list) or not refs
                            or len(set(refs)) != len(refs) or not set(refs) <= set(neighbors)):
                        raise ValueError(f"Duplicate layer or invalid adjacent references for {cls} on {layer}")
                    self.targets[cls, layer] = width, gap, tuple(refs), floor
            if not any(name == cls for name, _ in self.targets):
                raise ValueError(f"No layer targets for {cls}")
        if not self.targets:
            raise ValueError("No layer targets are declared")

    @staticmethod
    def number(value, *, zero=False):
        if (type(value) not in (int, float) or not math.isfinite(value)
                or value < 0 or (value == 0 and not zero)):
            raise ValueError(f"Invalid impedance profile dimension or target: {value!r}")
        return value

    def check_stack(self, path):
        setup, = parse(path.read_text(encoding="utf-8-sig")).children("setup")
        stack, = setup.children("stackup")
        native = [row for row in stack.children("layer")
                  if row.children("type")[0].atoms()[1].value.lower() in {"copper", "prepreg", "core"}]
        for row, expected in strict_zip(native, self.physical):
            name = row.atoms()[1].value
            materials = [v.atoms()[1].value for v in row.children("material")]
            thickness = sum(float(v.atoms()[1].value) for v in row.children("thickness"))
            if (name != expected["layer"] or row.children("type")[0].atoms()[1].value.lower() != expected["type"].lower()
                    or materials != expected.get("materials", [])
                    or not math.isclose(thickness, self.number(expected["thickness"]), abs_tol=1e-6)):
                raise ValueError(f"Saved physical stack differs at {name}")

    def rules(self):
        lines = ["# BEGIN AUTOPCB CONTROLLED IMPEDANCE"]
        for (cls, layer), (width, gap, _, floor) in self.targets.items():
            if not re.fullmatch(r"(?:[FB]|In\d+)\.Cu", layer):
                raise ValueError(f"Expected a standard copper layer name: {layer}")
            minimum = f"(min {floor:g}mm) " if floor is not None else ""
            lines.extend([f'(rule "{cls} {self.ohms[cls]:g} ohm {layer}"', f'  (layer "{layer}")',
                          f'''  (condition "A.hasNetclass('{cls}')")''',
                          f'  (constraint track_width {minimum}(opt {width:g}mm))',
                          f'  (constraint diff_pair_gap (opt {gap:g}mm))'])
            if floor is not None:
                lines.append(f'  (constraint connection_width (min {floor:g}mm))')
            lines.append(")")
        return "\n".join([*lines, "# END AUTOPCB CONTROLLED IMPEDANCE"])


class PairAudit:
    def __init__(self, project, stack):
        self.paths, self.stack = project, stack
        stack.check_stack(project.board)
        self.board = pcbnew.LoadBoard(str(project.board))
        if not self.board or self.board.GetCopperLayerCount() != stack.data["copper_layers"]:
            raise ValueError("KiCad could not load the specified copper stack")
        names = {str(name) for name in self.board.GetNetsByName() if name}
        self.pairs = declared_pairs(project.root, names)
        if not self.pairs:
            raise ValueError("Record a reviewed, nonempty differential_pairs list in autopcb.project.json")
        self.classes = {name: str(self.board.FindNet(name).GetNetClassName()) for name in names}
        reviewed = {n for pair in self.pairs for n in pair}
        omitted = {n for n in names if self.classes[n] in stack.ohms} - reviewed
        if project.settings.is_file():
            definitions = project.settings.read_bytes()
            if "net_settings" in json.loads(definitions.decode("utf-8-sig")):
                omitted.update(high_speed_nets(definitions, names) - reviewed)
        if omitted:
            raise ValueError("Declared pairs omit nets in a configured class: " + ", ".join(sorted(omitted)))
        configured = read_profile(project.root).get("reference_net", "GND")
        if not isinstance(configured, str) or not configured:
            raise ValueError("A reference net name is required")
        matches = names & {configured, "/" + configured}
        if len(matches) != 1 or matches & reviewed:
            raise ValueError("Missing, ambiguous or conflicting reference net")
        reference = matches.pop()
        self.planes = defaultdict(list)
        for zone in self.board.Zones():
            if not zone.GetIsRuleArea() and zone.GetNetname() == reference:
                for layer in zone.GetLayerSet().CuStack():
                    self.planes[self.board.GetLayerName(layer)].append(zone.GetFilledPolysList(layer))
        self.traces = defaultdict(lambda: defaultdict(list))
        self.unsupported = []
        self.vias = defaultdict(int)
        for item in self.board.GetTracks():
            net = str(item.GetNetname())
            if net not in reviewed:
                continue
            if isinstance(item, pcbnew.PCB_VIA):
                self.vias[net] += 1
            elif isinstance(item, pcbnew.PCB_ARC):
                self.unsupported.append(item.m_Uuid.AsString())
            else:
                self.traces[net][self.board.GetLayerName(item.GetLayer())].append(Trace.from_item(item))

    def covered(self, layers, points):
        coords = [pcbnew.VECTOR2I(round(x * 1e6), round(y * 1e6)) for x, y in points]
        return all(any(all(poly.Contains(p, i) for p in coords)
                       for poly in self.planes[layer] for i in range(poly.OutlineCount())) for layer in layers)

    def measure(self, tracks, mates, target):
        width, gap, refs, _ = target
        paired = referenced = 0.0
        total = sum(t.length for t in tracks)
        for trace in tracks:
            if abs(trace.width - width) > self.stack.width_tol + 1e-9:
                continue
            matched, grounded = [], []
            for mate in mates:
                if abs(mate.width - width) > self.stack.width_tol + 1e-9:
                    continue
                overlap = trace.overlap(mate, gap, self.stack.gap_tol)
                if overlap is None:
                    continue
                lo, hi, d = overlap
                matched.append((lo, hi))
                left, right = sorted((0.0, d))
                if d < 0:
                    left -= mate.width / 2
                    right += trace.width / 2
                else:
                    left -= trace.width / 2
                    right += mate.width / 2
                offsets = (left, (left + min(0.0, d)) / 2, 0.0, d / 2, d, (right + max(0.0, d)) / 2, right)
                n = max(1, math.ceil((hi - lo) / self.stack.step))
                size = (hi - lo) / n
                for k in range(n):
                    start, end = lo + k * size, lo + (k + 1) * size
                    if all(self.covered(refs, [trace.point(s, off) for off in offsets])
                           for s in (start, (start + end) / 2, end)):
                        grounded.append((start, end))
            paired += trace.union_length(matched)
            referenced += trace.union_length(grounded)
        return {"straight_mm": total, "matched_mm": paired, "referenced_mm": referenced,
                "matches": total > 0 and total - paired < 1e-8 and total - referenced < 1e-8}

    def run(self):
        results, issues = [], []
        for a, b in self.pairs:
            cls = self.classes[a]
            if cls != self.classes[b] or cls not in self.stack.ohms:
                raise ValueError(f"Both nets of {a} / {b} must belong to one configured net class")
            layers = self.traces[a].keys() | self.traces[b].keys()
            if not layers:
                issues.append(f"No straight routing measured for {a} / {b}")
            rows = {}
            for layer in sorted(layers):
                target = self.stack.targets.get((cls, layer))
                if target is None:
                    issues.append(f"No {cls} targets for routed layer {layer}")
                    continue
                sides = {n: self.measure(self.traces[n][layer], self.traces[m][layer], target)
                         for n, m in ((a, b), (b, a))}
                rows[layer] = {"width_mm": target[0], "gap_mm": target[1], "references": target[2], "nets": sides}
                if not all(s["matches"] for s in sides.values()):
                    issues.append(f"Review width, gap or sampled reference on {a} / {b}, {layer}")
            results.append({"nets": (a, b), "target_ohms": self.stack.ohms[cls], "layers": rows})
        if self.unsupported:
            issues.append(f"{len(self.unsupported)} curved segments could not be screened")
        return {"stackup": self.stack.identifier, "pairs": results, "issues": issues, "vias": dict(self.vias),
                "unsupported_ids": self.unsupported, "geometry_matches": not issues,
                "electrically_verified": False}


class ImpedanceCommand:
    def __call__(self, argv=None):
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--project", required=True, type=Path)
        parser.add_argument("--stackup", type=Path, help="Engineering stackup JSON (defaults to STACKUP_CURRENT.json)")
        parser.add_argument("--emit-rules", action="store_true", help="Print KiCad routing targets")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--check-reference", action="store_true", help="Return 2 for incomplete geometry coverage")
        args = parser.parse_args(argv)
        try:
            paths = ProjectPaths(args.project)
            spec = args.stackup or data_path(paths.root, "STACKUP_CURRENT.json")
            stack = StackProfile(json.loads(spec.read_text(encoding="utf-8-sig")))
            if args.emit_rules:
                print(stack.rules())
                return 0
            report = PairAudit(paths, stack).run()
        except (KeyError, ValueError, TypeError, IndexError, OSError) as exc:
            print(f"Impedance review could not run: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"{report['stackup']} straight-pair geometry")
            for pair in report["pairs"]:
                for layer, row in pair["layers"].items():
                    for name, side in row["nets"].items():
                        print(f"{name}  {layer}  {side['straight_mm']:.3f} mm straight  "
                              f"{side['matched_mm']:.3f} mm paired  {side['referenced_mm']:.3f} mm referenced")
            for issue in report["issues"]:
                print(issue)
            print("This screen measures layout geometry. Manufactured impedance requires separate verification.")
        return 2 if args.check_reference and not report["geometry_matches"] else 0


main = ImpedanceCommand()

if __name__ == "__main__":
    raise SystemExit(main())
