"""A development routing scope must stay explicit and preserve release gates."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pcb.checkpoints import read_config
from pcb.net_routing.protection import unqualified_working_nets
from pcb.net_routing.controller import encode_checkpoint, rebind_exact_saved_checkpoint
from review.refresh_gallery import ReviewGallery


class WorkingScopeTests(unittest.TestCase):
    def test_explicit_rule_adoption_still_protects_the_saved_board_and_other_inputs(
        self,
    ):
        import hashlib

        board = b"exact saved board"
        original = {
            "v4.1.kicad_pcb": hashlib.sha256(board).hexdigest(),
            "v4.1.kicad_dru": "old-rules",
            "v4.1.kicad_sch": "schematic",
        }
        checkpoint = encode_checkpoint(board, original, {}, "policy")
        changed = {**original, "v4.1.kicad_dru": "new-rules"}
        rebound = rebind_exact_saved_checkpoint(
            checkpoint, board, changed, {}, "policy", allow_rule_changes=True
        )
        self.assertEqual(rebound["inputs"], changed)
        with self.assertRaises(RuntimeError):
            rebind_exact_saved_checkpoint(checkpoint, board, changed, {}, "policy")
        with self.assertRaises(RuntimeError):
            rebind_exact_saved_checkpoint(
                checkpoint,
                board,
                {**changed, "v4.1.kicad_sch": "changed"},
                {},
                "policy",
                allow_rule_changes=True,
            )
        with self.assertRaises(RuntimeError):
            rebind_exact_saved_checkpoint(
                checkpoint,
                b"other board",
                changed,
                {},
                "policy",
                allow_rule_changes=True,
            )

    def test_only_the_exact_published_preview_can_rebind_its_source(self):
        import hashlib

        old = {"v4.1.kicad_pcb": "old", "v4.1.kicad_sch": "schematic"}
        board = b"retained candidate"
        checkpoint = encode_checkpoint(board, old, {"code": "before"}, "policy")
        current = {**old, "v4.1.kicad_pcb": hashlib.sha256(board).hexdigest()}
        rebound = rebind_exact_saved_checkpoint(
            checkpoint, board, current, {"code": "after"}, "policy"
        )
        self.assertEqual(rebound["inputs"], current)
        self.assertEqual(
            rebound["candidate_zlib_base64"], checkpoint["candidate_zlib_base64"]
        )
        for changed, data, policy in (
            ({**current, "v4.1.kicad_sch": "edited"}, board, "policy"),
            (current, b"different board", "policy"),
            (current, board, "different policy"),
        ):
            with self.assertRaises(RuntimeError):
                rebind_exact_saved_checkpoint(checkpoint, data, changed, {}, policy)

    def test_working_scope_keeps_pairs_nc_and_explicit_exclusions_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "autopcb/config/autopcb.json"
            config.parent.mkdir(parents=True)
            policy = {
                "schema": 1,
                "max_pins": 3,
                "working_scope": {
                    "kind": "connectivity",
                    "classes": ["CORE_SIGNAL", "CORE_POWER"],
                    "manufacturing_approved": False,
                },
            }
            config.write_text(json.dumps(policy))
            classes = {
                "": "Default",
                "DATA": "USB_DATA",
                "SIGNAL": "CORE_SIGNAL",
                "POWER": "CORE_POWER",
                "unconnected-X": "CORE_SIGNAL",
                "UNKNOWN": "Default",
            }
            board = SimpleNamespace(
                GetFileName=lambda: str(root / "board.kicad_pcb"),
                GetNetsByName=lambda: classes,
                FindNet=lambda name: SimpleNamespace(
                    GetNetClassName=lambda: classes[name]
                ),
            )
            blocked = unqualified_working_nets(board, ("SIGNAL",))
            self.assertEqual(blocked, {"DATA", "SIGNAL", "unconnected-X", "UNKNOWN"})
            policy["working_scope"]["manufacturing_approved"] = True
            config.write_text(json.dumps(policy))
            with self.assertRaises(ValueError):
                read_config(config)

    def test_six_layer_gallery_requires_both_added_layer_views(self):
        layers = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]
        pdfs, pngs, active = ReviewGallery.required_outputs(layers)
        self.assertEqual((len(pdfs), len(pngs)), (9, 13))
        self.assertTrue(
            {"PCB_In3.pdf", "PCB_In4.pdf", "PCB_In3.png", "PCB_In4.png"} <= active
        )


if __name__ == "__main__":
    unittest.main()
