"""Layer-label and soft-cost checkpoint rebasing must preserve the design."""

import hashlib
import json
import unittest

from pcb.net_routing.controller import encode_checkpoint
from pcb.net_routing.layer_rebase import (
    LayerAnnotations,
    LayerPolicy,
    rebase_checkpoint,
)


class LayerRebaseTests(unittest.TestCase):
    def setUp(self):
        self.old = (
            b'(kicad_pcb (layers (0 "F.Cu" signal) (4 "In1.Cu" power) '
            b'(6 "In2.Cu" signal) (2 "B.Cu" signal)) '
            b'(setup (stackup (layer "F.Cu" (thickness 0.035)))) '
            b'(footprint "fixed" (layer "F.Cu") (at 1 2)))'
        )
        self.new = self.old.replace(b'"In1.Cu" power', b'"In1.Cu" signal')
        self.candidate = self.old.replace(b"(at 1 2)", b'(at 1 2) (uuid "c")')
        self.policy = {
            "route": {
                "layers": ["B.Cu", "In2.Cu", "In1.Cu", "F.Cu"],
                "layer_penalties": {"F.Cu": 0.25},
                "width_mm": 0.25,
            },
            "max_pins": 3,
        }

    def test_only_changed_layer_roles_are_applied_to_candidate(self):
        changed = LayerAnnotations.rebase(self.old, self.new, self.candidate)
        self.assertEqual(
            changed, self.candidate.replace(b'"In1.Cu" power', b'"In1.Cu" signal')
        )
        self.assertIn(b"(thickness 0.035)", changed)

    def test_any_non_annotation_board_change_is_rejected(self):
        for new in (
            self.new.replace(b"(at 1 2)", b"(at 3 2)"),
            self.new.replace(b"(thickness 0.035)", b"(thickness 0.061)"),
        ):
            with self.assertRaisesRegex(ValueError, "only.*layer"):
                LayerAnnotations.rebase(self.old, new, self.candidate)

    def test_candidate_must_match_old_layer_roles(self):
        with self.assertRaisesRegex(ValueError, "candidate.*layer"):
            LayerAnnotations.rebase(self.old, self.new, self.new)

    def test_only_soft_layer_costs_and_order_may_change(self):
        policy = {
            "route": {
                "layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                "layer_penalties": {},
                "width_mm": 0.25,
            },
            "max_pins": 3,
        }
        self.assertTrue(LayerPolicy.compatible(self.policy, policy))
        for mutation in (
            dict(policy["route"], width_mm=0.15),
            dict(policy["route"], layers=["F.Cu", "B.Cu"]),
        ):
            self.assertFalse(
                LayerPolicy.compatible(self.policy, {**policy, "route": mutation})
            )

    def test_checkpoint_rebase_checks_all_other_sources_and_hard_policy(self):
        old_policy = json.dumps(self.policy).encode()
        new_policy = json.dumps(
            {
                "route": {
                    "layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                    "layer_penalties": {},
                    "width_mm": 0.25,
                },
                "max_pins": 3,
            }
        ).encode()

        def digest(value):
            return hashlib.sha256(value).hexdigest()

        inputs = {"v4.1.kicad_pcb": digest(self.old), "v4.1.kicad_sch": "unchanged"}
        checkpoint = encode_checkpoint(
            self.candidate, inputs, {"old": "tool"}, digest(old_policy)
        )
        current = {**inputs, "v4.1.kicad_pcb": digest(self.new)}
        result = rebase_checkpoint(
            checkpoint,
            current,
            {"new": "tool"},
            self.old,
            self.new,
            old_policy,
            new_policy,
        )
        self.assertEqual(
            result, self.candidate.replace(b'"In1.Cu" power', b'"In1.Cu" signal')
        )
        with self.assertRaises(RuntimeError):
            rebase_checkpoint(
                checkpoint,
                {**current, "v4.1.kicad_sch": "changed"},
                {},
                self.old,
                self.new,
                old_policy,
                new_policy,
            )
        hard_change = new_policy.replace(b'"width_mm": 0.25', b'"width_mm": 0.15')
        with self.assertRaisesRegex(ValueError, "policy"):
            rebase_checkpoint(
                checkpoint, current, {}, self.old, self.new, old_policy, hard_change
            )

    def test_rebase_requires_an_explicit_renewal_and_both_preimages(self):
        from pathlib import Path
        from pcb.net_routing.runner import run_project

        with self.assertRaisesRegex(ValueError, "renew.*previous"):
            run_project(Path("."), previous_board=Path("old.kicad_pcb"))
        with self.assertRaisesRegex(ValueError, "renew.*previous"):
            run_project(Path("."), renew="a" * 32, previous_board=Path("old.kicad_pcb"))

    def test_layer_order_and_penalty_validation_fails_closed(self):
        for mutation in (
            {"layers": ["F.Cu", "F.Cu"], "layer_penalties": {}},
            {"layers": ["F.Cu"], "layer_penalties": {"F.Cu": -1}},
            {"layers": ["F.Cu"], "layer_penalties": {"F.Cu": float("nan")}},
        ):
            self.assertFalse(
                LayerPolicy.compatible({"route": mutation}, {"route": mutation})
            )


if __name__ == "__main__":
    unittest.main()
