"""Explicit code renewal preserves a checked candidate, not its old plan."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.native_command import NativeResourceError
from pcb.net_routing.controller import decode_checkpoint, encode_checkpoint
from pcb.net_routing import runner


class RenewalTests(unittest.TestCase):
    def test_unexpected_error_preserves_the_last_committed_checkpoint_and_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run.json"
            committed = {
                "status": "running",
                "queue": [[{"nets": [{"name": "SIGNAL"}]}]],
                "checkpoint": {"candidate": "verified"},
            }
            path.write_text(json.dumps(committed), encoding="utf-8")
            working = json.loads(json.dumps(committed))
            with self.assertRaisesRegex(ValueError, "unexpected rule failure"):
                with runner._record_failures(path, working):
                    working["queue"].pop(0)
                    working["checkpoint"] = {"candidate": "unverified"}
                    raise ValueError("unexpected rule failure")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["failure"], "unexpected rule failure")
            self.assertEqual(saved["queue"], committed["queue"])
            self.assertEqual(saved["checkpoint"], committed["checkpoint"])

    def test_only_code_changes_are_eligible_for_renewal(self):
        checkpoint = encode_checkpoint(
            b"board", {"source": "a"}, {"code": "old"}, "policy"
        )
        self.assertEqual(
            decode_checkpoint(
                checkpoint,
                {"source": "a"},
                {"code": "new"},
                "policy",
                allow_tool_changes=True,
            ),
            b"board",
        )
        for inputs, policy in (({"source": "b"}, "policy"), ({"source": "a"}, "new")):
            with self.assertRaisesRegex(RuntimeError, "inputs or policy changed"):
                decode_checkpoint(
                    checkpoint, inputs, {"code": "new"}, policy, allow_tool_changes=True
                )
        checkpoint["candidate_sha256"] = "tampered"
        with self.assertRaisesRegex(RuntimeError, "candidate"):
            decode_checkpoint(
                checkpoint,
                {"source": "a"},
                {"code": "new"},
                "policy",
                allow_tool_changes=True,
            )

    def test_native_renewal_revalidates_replans_and_preserves_the_previous_run(self):
        from verification.test_autoroute_batch import CombinedBatchTests
        from verification.test_net_routing import NativeNetRoutingTests

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = CombinedBatchTests().project(project)
            schematic = project / "v4.1.kicad_sch"
            schematic.write_text(
                '(kicad_sch (version 20250114) (generator "eeschema") '
                '(uuid "6b6b7707-0425-4195-9c79-1824e2f1a24e") (paper "A4") '
                '(lib_symbols) (sheet_instances (path "/" (page "1"))) (embedded_fonts no))'
            )
            config = NativeNetRoutingTests.config()
            (project / "config").mkdir()
            (project / "config" / "autopcb.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "max_pins": 3,
                        "route": config["shared"],
                        "search": config["search"],
                        "include_nets": ["SIGNAL"],
                    }
                )
            )
            native_before = source.read_bytes(), schematic.read_bytes()
            first = runner.run_project(project, max_rounds=1, workers=1)
            records = project / "results" / "autoroute"
            old_path = records / f"{first['run_id']}.json"
            old = json.loads(old_path.read_text())

            # A resource failure during normal resume is recorded without
            # consuming work or replacing the last accepted native candidate.
            with mock.patch.object(
                runner,
                "verify_resume_board",
                side_effect=NativeResourceError("budget exhausted"),
            ):
                with self.assertRaises(NativeResourceError):
                    runner.run_project(project, resume=first["run_id"], max_rounds=1)
            failed = json.loads(old_path.read_text())
            self.assertEqual(failed["status"], "resource_blocked")
            self.assertEqual(failed["queue"], old["queue"])
            self.assertEqual(failed["checkpoint"], old["checkpoint"])

            failed["checkpoint"]["tools"]["old_code_marker"] = "old"
            # A deliberately invalid historical request must not be replayed.
            failed["queue"] = [[{"old_request": "never execute"}]]
            old_path.write_text(json.dumps(failed))
            previous_bytes = old_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "tools changed"):
                runner.run_project(project, resume=first["run_id"], max_rounds=1)
            files_before = set(records.glob("*.json"))
            with mock.patch.object(
                runner,
                "verify_resume_board",
                side_effect=RuntimeError("native rejection"),
            ):
                with self.assertRaisesRegex(RuntimeError, "native rejection"):
                    runner.run_project(project, renew=first["run_id"], max_rounds=1)
            self.assertEqual(set(records.glob("*.json")), files_before)
            self.assertEqual(old_path.read_bytes(), previous_bytes)

            renewed = runner.run_project(
                project, renew=first["run_id"], max_rounds=1, workers=1
            )
            self.assertNotEqual(renewed["run_id"], first["run_id"])
            self.assertEqual(renewed["renewed_from"], first["run_id"])
            self.assertNotIn("failure", renewed)
            self.assertFalse(renewed["publication_enabled"])
            self.assertEqual(old_path.read_bytes(), previous_bytes)
            self.assertEqual(
                (source.read_bytes(), schematic.read_bytes()), native_before
            )
            renewed_record = json.loads(
                (records / f"{renewed['run_id']}.json").read_text()
            )
            self.assertNotIn("old_code_marker", renewed_record["checkpoint"]["tools"])
            self.assertNotIn("old_request", json.dumps(renewed_record["queue"]))
