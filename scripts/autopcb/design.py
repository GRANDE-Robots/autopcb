"""Turn requirements and a local engineering catalog into a reviewable proposal."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import math
from pathlib import Path
import re
from uuid import UUID, uuid5

from core.kicad_sexpr import quote
from core.compat import strict_zip


def digest(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def catalog(path: Path | None = None) -> dict:
    resource = path or importlib.resources.files("autopcb").joinpath("catalog.json")
    data = json.loads(resource.read_text(encoding="utf-8-sig"))
    if data.get("schema_version") != 1 or not all(isinstance(data.get(key), dict) for key in ("parts", "designs")):
        raise ValueError("Expected a version 1 AutoPCB design catalog")
    return data


def propose(text: str, *, catalog_file: Path | None = None) -> dict:
    if not text.strip() or len(text) > 32_000:
        raise ValueError("Supply a nonempty requirements document of at most 32,000 characters")
    data = catalog(catalog_file)
    normalized = " ".join(text.casefold().replace("²", "2").split())
    matches = [name for name, row in data["designs"].items()
               if any(keyword.casefold() in normalized for keyword in row["keywords"])]
    limits = {
        "supply_v": {float(v) for v in re.findall(r"\b(\d+(?:\.\d+)?)\s*v(?:olts?)?\b", normalized)},
        "layers": {int(v) for v in re.findall(r"\b(\d+|two|four|six|eight)[ -]*layers?\b", normalized)
                   if v.isdigit()},
    }
    for token, count in (("two", 2), ("four", 4), ("six", 6), ("eight", 8)):
        if re.search(rf"\b{token}[ -]*layers?\b", normalized):
            limits["layers"].add(count)
    number = r"([+-]?(?:\d+(?:\.\d+)?|\.\d+))"
    unit = r"(mm|cm|inch(?:es)?|in|mils?)"
    rectangles = re.finditer(rf"(?<![\w.]){number}\s*{unit}?\s*(?:[x×]|\bby\b)\s*{number}"
                             rf"(?:\s*{unit}\b)?(?!\w)", normalized)
    scales = {"mm": 1, "cm": 10, "in": 25.4, "inch": 25.4, "inches": 25.4, "mil": 0.0254, "mils": 0.0254}
    unresolved = []
    dimensions = []
    maximum_requested = False
    for match in rectangles:
        w, u, h, v = match.groups()
        if u or v:
            dimensions.append([float(w) * scales[u or v], float(h) * scales[v or u]])
            maximum_requested = bool(
                re.search(r"\b(?:at most|up to|no larger than|within|max(?:imum)?(?: board size)?)"
                          r"\s*(?:is |of |a |[:=]\s*)?$", normalized[:match.start()])
                or re.match(r"\s*(?:max(?:imum)?\b|or smaller\b)", normalized[match.end():]))
        elif not re.fullmatch(r"0x[\da-f]+", match.group()):
            unresolved.append("Give board dimensions in mm, cm, inches or mils.")
    if len(matches) != 1:
        unresolved.append("Choose one supported circuit function or supply a catalog for this design.")
    if len(limits["supply_v"]) != 1:
        unresolved.append("Specify one nominal external supply in volts.")
    if len(limits["layers"]) > 1 or len(dimensions) > 1:
        unresolved.append("Specify a single copper layer count and board size.")
    result = {"schema_version": 1, "kind": "design_proposal", "requirements": text.strip(), "unresolved": unresolved}
    if len(matches) == 1:
        key = matches[0]
        recipe = data["designs"][key]
        if len(limits["supply_v"]) == 1 and not recipe["voltage_v"][0] <= next(iter(limits["supply_v"])) <= recipe["voltage_v"][1]:
            unresolved.append("The requested supply is outside the characterized circuit range.")
        if len(limits["layers"]) == 1 and next(iter(limits["layers"])) not in recipe["layers"]:
            unresolved.append(f"The characterized layout supports layer counts {recipe['layers']}.")
        size = dimensions[0] if dimensions else recipe["size_mm"]
        fixed = re.search(r"\b(?:fixed|existing|nominal|catalog|default) (?:board size|outline|board dimensions)\b",
                          normalized)
        compact = "compact_outline" in recipe and not fixed and (not dimensions or maximum_requested)
        if compact and maximum_requested:
            size = [min(limit, nominal) for limit, nominal in strict_zip(size, recipe["size_mm"])]
        if not all(math.isfinite(upper) and 0 < low <= upper
                   for upper, low in strict_zip(size, recipe["minimum_mm"])):
            unresolved.append("The requested outline is smaller than the catalog layout envelope.")
        if key == "usb" and re.search(r"\b(high[ -]speed|super[ -]speed|usb\s*3|480\s*m)", normalized):
            unresolved.append("This reference circuit covers full-speed USB 2.0 only.")
        result.update(design=key, description=recipe["description"],
                      supply_v=next(iter(limits["supply_v"])) if len(limits["supply_v"]) == 1 else None,
                      layers=next(iter(limits["layers"])) if len(limits["layers"]) == 1 else recipe["layers"][0],
                      size_mm=size, assumptions=recipe["assumptions"], power_flags=recipe["power_flags"],
                      differential_pairs=recipe.get("differential_pairs", []),
                      components={ref: {**data["parts"][values["part"]], "symbol": values["part"],
                                        "nets": values["pins"], "position": values["position"],
                                        **({"side": values["side"]} if "side" in values else {})}
                                  for ref, values in recipe["parts"].items()})
        if "pair_layout" in recipe:
            result["pair_layout"] = recipe["pair_layout"]
        if compact:
            outline = recipe["compact_outline"]
            if not isinstance(outline, dict):
                raise ValueError("Expected a compact outline configuration in the catalog.")
            groups = outline.get("movable_groups")
            margin = outline.get("margin_mm")
            if (type(margin) not in (int, float) or not math.isfinite(margin) or margin <= 0
                    or not isinstance(groups, list)
                    or any(not isinstance(g, list) or not g or
                           any(not isinstance(ref, str) or ref not in recipe["parts"] for ref in g) for g in groups)
                    or len({ref for g in groups for ref in g}) != sum(map(len, groups))):
                raise ValueError("The catalog needs a positive edge margin and disjoint groups of known parts.")
            result["compact_outline"] = {"minimum_mm": recipe["minimum_mm"], "margin_mm": margin,
                                         "movable_groups": groups}
    result["status"] = "blocked" if unresolved else "proposed"
    result["proposal_id"] = digest(result)
    return result


def validated(proposal: dict, approval: str) -> dict:
    body = {key: value for key, value in proposal.items() if key != "proposal_id"}
    if (body.get("schema_version") != 1 or body.get("kind") != "design_proposal"
            or body.get("status") != "proposed" or body.get("unresolved")
            or proposal.get("proposal_id") != approval or digest(body) != approval):
        raise ValueError("Only a complete proposal with matching explicit approval can create a project.")
    return body


def _f(number: float) -> str:
    return f"{number:.4f}".rstrip("0").rstrip(".") or "0"


def schematic(proposal: dict, title: str) -> tuple[str, str, dict]:
    """Generate the schematic and its KiCad symbol library from the same pin model."""
    seed = UUID(proposal["proposal_id"][:32])
    def uid(name: str) -> str:
        return str(uuid5(seed, name))

    root = uid("schematic")
    library = {}
    parts = dict(proposal["components"])
    for index, name in enumerate(proposal["power_flags"], 1):
        parts[f"#FLG{index:02}"] = {"symbol": "power_flag", "value": "PWR_FLAG", "footprint": "",
                                  "pins": {"1": ["Supply", "power_out"]}, "nets": {"1": name}}

    symbols = []
    links = {}
    for index, (reference, part) in enumerate(parts.items()):
        key = part["symbol"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            raise ValueError(f"Invalid catalog symbol identifier: {key}")
        symbol_id = f"AutoPCB:{key}"
        pins = part["pins"]
        if pins.keys() != part["nets"].keys():
            raise ValueError(f"{reference} does not have an explicit assignment for every pin")
        is_flag = reference.startswith("#")
        origin_x, origin_y = 40.64 + 58.42 * (index % 3), 40.64 + 40.64 * (index // 3)
        symbol_uuid = uid(reference)
        props = {"Reference": reference, "Value": part["value"], "Footprint": part["footprint"],
                 "Datasheet": part.get("source", ""), "Manufacturer": part.get("manufacturer", ""),
                 "MPN": part.get("mpn", "")}
        instances = []
        for name, value in props.items():
            x, y = origin_x, origin_y - (4.445 if name == "Reference" else 1.905)
            hide = "" if name in ("Reference", "Value") else " (hide yes)"
            instances.append(f'(property {quote(name)} {quote(value)} (at {_f(x)} {_f(y)} 0)'
                             f' (effects (font (size 1.0 1.0)){hide}))')
        pin_geometry = []
        for pin_index, (number, (name, kind)) in enumerate(pins.items()):
            y = -2.54 * pin_index
            pin_geometry.append(f'(pin {kind} line (at -7.62 {_f(y)} 0) (length 2.54)'
                                f' (name {quote(name)} (effects (font (size 1.0 1.0))))'
                                f' (number {quote(number)} (effects (font (size 1.0 1.0)))))')
            instances.append(f'(pin {quote(number)} (uuid "{uid(reference + ":pin:" + number)}"))')
            end_x, end_y = origin_x - 7.62, origin_y - y
            net = part["nets"][number]
            if net is not None:
                label_x = end_x - 2.54
                symbols.append(f'(wire (pts (xy {_f(label_x)} {_f(end_y)}) (xy {_f(end_x)} {_f(end_y)}))'
                               f' (stroke (width 0) (type default)) (uuid "{uid(reference + ":wire:" + number)}"))')
                symbols.append(f'(label {quote(net)} (at {_f(label_x)} {_f(end_y)} 0)'
                               f' (effects (font (size 1.0 1.0)) (justify right bottom))'
                               f' (uuid "{uid(reference + ":label:" + number)}"))')
            else:
                symbols.append(f'(no_connect (at {_f(end_x)} {_f(end_y)})'
                               f' (uuid "{uid(reference + ":nc:" + number)}"))')
        base = (f'(pin_names (offset 0.5)) (exclude_from_sim no) (in_bom {"no" if is_flag else "yes"})'
                f' (on_board {"no" if is_flag else "yes"})'
                f' (property "Reference" "{reference.rstrip("0123456789")}" (at 0 3.81 0) (effects (font (size 1 1))))'
                f' (property "Value" {quote(part["value"])} (at 0 1.905 0) (effects (font (size 1 1))))'
                f' (property "Footprint" {quote(part["footprint"])} (at 0 0 0) (effects (font (size 1 1)) (hide yes)))'
                f' (symbol "{key}_0_1" (rectangle (start -5.08 1.27) (end 5.08 {_f(-2.54 * (len(pins)-1)-1.27)})'
                f' (stroke (width 0.254) (type default)) (fill (type background))))'
                f' (symbol "{key}_1_1" {" ".join(pin_geometry)})')
        if key in library and library[key] != base:
            raise ValueError(f"Conflicting definitions for catalog symbol {key}")
        library[key] = base
        symbols.append(f'(symbol (lib_id "{symbol_id}") (at {_f(origin_x)} {_f(origin_y)} 0) (unit 1)'
                       f' (in_bom {"no" if is_flag else "yes"}) (on_board {"no" if is_flag else "yes"}) (dnp no)'
                       f' (uuid "{symbol_uuid}") {" ".join(instances)}'
                       f' (instances (project {quote(title)} (path "/{root}" (reference {quote(reference)}) (unit 1)))))')
        if not is_flag:
            links[reference] = [root, symbol_uuid]
    embedded = "\n".join(f'(symbol "AutoPCB:{key}" {body})' for key, body in library.items())
    sym_file = "\n".join(f'(symbol "{key}" {body})' for key, body in library.items())
    document = (f'(kicad_sch (version 20250114) (generator "AutoPCB") (uuid "{root}") (paper "A4")\n'
                f'(lib_symbols {embedded})\n' + "\n".join(symbols) + '\n(sheet_instances (path "/" (page "1")))\n)\n')
    return document, f'(kicad_symbol_lib (version 20231120) (generator "AutoPCB")\n{sym_file}\n)\n', links
