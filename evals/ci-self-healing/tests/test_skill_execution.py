"""No-model controls using real workspace_check receipts, not success prose."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import EvalError, changes, digest, json_bytes, read_regular, snapshot, write_new
from repair_support import (EXECUTION_CONTRACT, LEGACY_EXECUTION_CONTRACT, RECOVERY_EXECUTION_CONTRACT,
                            execution_contract, source_state)
from skill_execution import audit_execution, is_build, workspace_result

SKILL = ROOT.parents[1] / "skills/ci-self-healing"


class SkillExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skill-execution-")
        self.addCleanup(self.temp.cleanup)
        self.trial = Path(self.temp.name).resolve()
        self.visible = self.trial / "visible"
        self.repo = self.visible / "scene/sourcecode"
        self.out = self.visible / "output"
        self.out.mkdir(parents=True)
        write_new(self.repo / "src/main/java/Example.java", "class Example {}\n")
        write_new(self.repo / ".gitignore", "target/\n")
        write_new(self.repo / "target/preexisting.txt", "job-owned\n")
        self.skill = self.trial / "runtime/skills/ci-self-healing"
        self.events = []
        self.previous = snapshot(self.visible)
        write_new(self.skill / "assets/execution-resources.json", read_regular(SKILL / "assets/execution-resources.json"))
        for name in ("SKILL.md", "references/diagnosis-and-repair.md", "references/repair-basis.md",
                     "references/repair-and-verification.md", "references/workspace-cleanup.md"):
            self.load_resource(name)

    def load_resource(self, name):
        data = read_regular(SKILL / name)
        if not (self.skill / name).exists():
            write_new(self.skill / name, data)
        tool = "load_skill" if name == "SKILL.md" else "read_skill_resource"
        args = {"skill_name": "ci-self-healing"}
        if tool != "load_skill":
            args["resource_name"] = name
        self.call(tool, args, data.decode())

    def truncate_resource(self, resource):
        for i, event in enumerate(self.events):
            if event["kind"] == "InvocationToolCallStart" and event["payload"]["args"].get("resource_name") == resource:
                self.events[i + 1]["payload"]["result"] = "[truncated]"

    def event(self, kind, payload):
        current = snapshot(self.visible)
        payload.update(source_state_sha256=digest(json_bytes(source_state(current))),
                       changed_paths_since_previous_event=changes(self.previous, current))
        self.previous = current
        self.events.append({"seq": len(self.events) + 1, "kind": kind, "payload": payload})

    def call(self, name, args, value, action=None):
        call_id = str(len(self.events))
        self.event("InvocationToolCallStart", {"tool_name": name, "args": args, "call_id": call_id})
        if action:
            action()
        self.event("InvocationToolCallResult", {"tool_name": name, "call_id": call_id, "result": value})

    def workspace(self, operation, label="one", **overrides):
        args = [operation, "--root", str(self.repo), "--manifest", str(self.out / (label + ".json")),
                "--invocation-id", "test", "--candidate-id", "candidate"]
        if operation == "check":
            args += ["--manifest-sha256", self.sha]
        for flag, value in overrides.items():
            args[args.index("--" + flag.replace("_", "-")) + 1] = value
        call_id = str(len(self.events))
        self.event("InvocationToolCallStart", {"tool_name": "run_skill_script", "call_id": call_id,
                    "args": {"skill_name": "ci-self-healing", "script_name": "scripts/workspace_check.py", "arguments": args}})
        p = subprocess.run([sys.executable, "-B", str(SKILL / "scripts/workspace_check.py"), *args], capture_output=True, text=True, timeout=10)
        self.event("InvocationToolCallResult", {"tool_name": "run_skill_script", "call_id": call_id,
                    "result": p.stdout + p.stderr + f"\n[exit_code: {p.returncode}]"})
        receipt = json.loads(p.stdout)
        if operation == "capture":
            self.sha = receipt["manifest_sha256"]
        return receipt

    def build(self):
        self.call("bash", {"command": "mvn -o -B compile"}, "BUILD SUCCESS\n[exit_code: 0]",
                  lambda: write_new(self.repo / "target/generated-sources/annotations/Example.class", b"synthetic"))

    def clean(self, dirs=True):
        (self.repo / "target/generated-sources/annotations/Example.class").unlink()
        if dirs:
            (self.repo / "target/generated-sources/annotations").rmdir()
            (self.repo / "target/generated-sources").rmdir()
        self.event("InvocationMessage", {"text": "cleaned"})

    def audit(self, expected="repair_caller", **kwargs):
        return audit_execution(self.trial, self.events, snapshot(self.visible), expected, **kwargs)

    def checks(self, expected="repair_caller"):
        return [r["check"] for r in self.audit(expected)["failures"]]

    def test_complete_native_flow_preserves_preexisting_and_passes(self):
        self.workspace("capture"); self.build(); self.clean(); self.workspace("check")
        self.assertEqual(self.audit()["failures"], [])
        self.assertEqual(self.audit()["gaps"], [])
        self.assertEqual((self.repo / "target/preexisting.txt").read_text(), "job-owned\n")
        self.assertEqual(self.audit()["dimensions"]["cleanup_evidence"]["status"], "confirmed")
        self.assertEqual(self.audit()["workspace_checks"][-1]["binding"], "confirmed")

    def capture_before_and_after_candidate(self):
        self.workspace("capture", "superseded")
        old_seq = self.events[-1]["seq"]
        old = self.out / "superseded.json"
        original = old.read_bytes()
        self.load_resource("references/workspace-recovery.md")
        self.call("edit_file", {"path": "Example.java"}, "done",
                  lambda: (self.repo / "src/main/java/Example.java").write_text("class Changed {}"))
        self.workspace("capture")
        return old, original, old_seq

    def test_candidate_then_single_capture_and_cleanup_batch_matches_first_check(self):
        # A Job may own both files and empty directories in an ignored output tree.
        old_empty = self.repo / "target/job-owned-empty"
        old_empty.mkdir()
        self.call("edit_file", {"path": "Example.java"}, "done",
                  lambda: (self.repo / "src/main/java/Example.java").write_text("class Changed {}"))
        self.workspace("capture")
        manifest = (self.out / "one.json").read_bytes()
        self.build()

        def cleanup_batch():
            # Explicit owned paths; no recursive deletion of the shared target root.
            (self.repo / "target/generated-sources/annotations/Example.class").unlink()
            (self.repo / "target/generated-sources/annotations").rmdir()
            (self.repo / "target/generated-sources").rmdir()

        self.call("bash", {"command": "rm target/generated-sources/annotations/Example.class && "
                  "rmdir target/generated-sources/annotations target/generated-sources"},
                  "[exit_code: 0]", cleanup_batch)
        self.assertEqual(self.workspace("check")["result"], "match")
        result = self.audit()
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["violations"], [])
        self.assertEqual([r["result"] for r in result["workspace_checks"]], ["captured", "match"])
        self.assertEqual((self.out / "one.json").read_bytes(), manifest)
        self.assertTrue(old_empty.is_dir())
        self.assertEqual((self.repo / "target/preexisting.txt").read_text(), "job-owned\n")
        self.assertEqual((self.repo / "src/main/java/Example.java").read_text(), "class Changed {}")

    def test_cleanup_batch_must_not_remove_preexisting_empty_directory(self):
        old_empty = self.repo / "target/job-owned-empty"
        old_empty.mkdir()
        self.workspace("capture"); self.build(); self.clean()
        self.call("bash", {"command": "rmdir target/job-owned-empty"}, "[exit_code: 0]", old_empty.rmdir)
        self.assertEqual(self.workspace("check")["result"], "mismatch")
        self.assertIn("final_native_cleanup_match_unconfirmed", self.checks())

    def test_superseded_capture_is_retained_while_current_capture_verifies_candidate(self):
        old, original, _ = self.capture_before_and_after_candidate()
        self.build(); self.clean(); self.workspace("check")
        result = self.audit()
        self.assertEqual(old.read_bytes(), original)
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["gaps"], [])
        self.assertTrue(all(r["binding"] == "confirmed" for r in result["workspace_checks"]))
        self.assertEqual(result["dimensions"]["cleanup_evidence"]["status"], "confirmed")
        self.assertEqual((self.repo / "target/preexisting.txt").read_text(), "job-owned\n")

    def test_deleting_superseded_capture_leaves_gap_despite_current_match(self):
        old, _, seq = self.capture_before_and_after_candidate()
        self.call("bash", {"command": "rm -f " + str(old)}, "[exit_code: 0]", old.unlink)
        self.build(); self.clean(); self.workspace("check")
        result = self.audit()
        self.assertEqual(result["workspace_checks"][-1]["result"], "match")
        self.assertEqual(result["workspace_checks"][-1]["binding"], "confirmed")
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["gaps"], [f"workspace_receipt_unconfirmed:E{seq:04d}"])
        self.assertEqual(result["dimensions"]["cleanup_evidence"]["status"], "unconfirmed")

    def test_replacing_superseded_capture_with_current_manifest_does_not_restore_binding(self):
        old, _, seq = self.capture_before_and_after_candidate()
        self.call("write_file", {"path": str(old)}, "done",
                  lambda: old.write_bytes((self.out / "one.json").read_bytes()))
        self.build(); self.clean(); self.workspace("check")
        result = self.audit()
        self.assertEqual(result["workspace_checks"][-1]["result"], "match")
        self.assertEqual(result["gaps"], [f"workspace_receipt_unconfirmed:E{seq:04d}"])
        self.assertEqual(result["dimensions"]["cleanup_evidence"]["status"], "unconfirmed")

    def test_deleted_historical_capture_blocks_recovery_even_after_rebuild_and_match(self):
        self.late_candidate()
        old = self.out / "one.json"
        self.call("bash", {"command": "rm -f " + str(old)}, "[exit_code: 0]", old.unlink)
        self.complete_recovery()
        result = self.audit()
        self.assertEqual(result["workspace_checks"][-1]["result"], "match")
        self.assertTrue(result["gaps"])
        self.assertEqual(result["recoveries"], [])
        self.assertIn("build_without_confirmed_native_capture", [f["check"] for f in result["failures"]])

    def test_report_mentioning_skill_path_is_not_a_file_access(self):
        self.call("write_file", {"path": str(self.out / "analysis-fix.md"),
                  "content": str(self.skill / "scripts/workspace_check.py")}, "done")
        self.assertEqual(self.audit("stop")["dimensions"]["native_protocol"]["status"], "passed")

    def test_stop_label_does_not_waive_cleanup_after_observed_build(self):
        self.build()
        self.assertEqual(self.audit("stop")["dimensions"]["cleanup_evidence"]["status"], "unconfirmed")

    def test_shell_match_is_observed_without_native_binding_or_protocol_pass(self):
        self.workspace("capture"); self.build(); self.clean(); self.workspace("check")
        # Same output, but delivered through the explicitly forbidden channel.
        for e in self.events:
            p = e["payload"]
            if p.get("tool_name") == "run_skill_script":
                p["tool_name"] = "bash"
                if "args" in p:
                    p["args"] = {"command": "python3 " + str(self.skill / "scripts/workspace_check.py")}
        result = self.audit()
        self.assertEqual(result["dimensions"]["native_protocol"]["status"], "failed")
        self.assertEqual(result["dimensions"]["workspace_observations"]["latest"]["result"], "match")
        self.assertEqual(result["dimensions"]["cleanup_evidence"]["status"], "unconfirmed")
        self.assertEqual(result["workspace_checks"], [])

    def test_expected_outputs_and_success_prose_do_not_override_mismatch(self):
        self.workspace("capture"); self.build(); self.workspace("check")
        self.event("InvocationMessage", {"text": "normal ignored outputs; V0 passed; local_verified"})
        self.assertIn("final_native_cleanup_match_unconfirmed", self.checks())

    def test_empty_ignored_directories_still_fail(self):
        self.workspace("capture"); self.build(); self.clean(dirs=False)
        self.assertEqual(self.workspace("check")["result"], "mismatch")
        self.assertIn("final_native_cleanup_match_unconfirmed", self.checks())

    def test_manual_cleanup_does_not_replace_prebuild_capture(self):
        self.build(); self.clean()
        self.assertIn("build_without_confirmed_native_capture", self.checks())
        self.assertIn("final_native_cleanup_match_unconfirmed", self.checks())

    def late_candidate(self):
        self.workspace("capture")
        self.call("edit_file", {"path": "Example.java"}, "done",
                  lambda: (self.repo / "src/main/java/Example.java").write_text("class Changed {}"))
        self.build()
        self.clean()
        self.assertEqual(self.workspace("check")["result"], "mismatch")

    def complete_recovery(self):
        self.load_resource("references/workspace-recovery.md")
        self.workspace("capture", "recovered")
        self.build(); self.clean(); self.workspace("check", "recovered")

    def test_proven_recovery_preserves_violation_without_blocking_cleanup(self):
        self.late_candidate(); self.complete_recovery()
        result = self.audit()
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["gaps"], [])
        self.assertEqual(len(result["violations"]), 1)
        recovery = result["recoveries"][0]
        self.assertEqual(recovery["violation"], result["violations"][0])
        self.assertLess(recovery["prebuild_seq"], recovery["capture_seq"])
        self.assertLess(recovery["capture_seq"], recovery["reverification_build_seqs"][0])
        self.assertLess(recovery["reverification_build_seqs"][-1], recovery["check_seq"])
        self.assertEqual(recovery["source_state_sha256"], digest(json_bytes(source_state(snapshot(self.visible)))))
        self.assertEqual(result["dimensions"]["cleanup_evidence"]["status"], "confirmed")
        self.assertEqual(result["dimensions"]["process_compliance"]["status"], "violations_observed")

    def test_missing_capture_can_recover_only_with_observed_reconstruction_and_rerun(self):
        self.build(); self.clean(); self.complete_recovery()
        self.assertEqual(self.audit()["failures"], [])
        self.assertEqual(len(self.audit()["recoveries"]), 1)

    def test_stale_capture_recovery_does_not_override_outer_workspace_guard(self):
        from grading import final_state_checks
        before = snapshot(self.visible)
        self.workspace("capture")
        write_new(self.repo / "target/new-input", "input added after capture")
        self.build(); self.clean(); self.complete_recovery()
        result = self.audit()
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["recoveries"][0]["violation"]["check"], "capture_not_current_at_build")
        self.assertIn("protected_inputs_or_cleanup", [f["check"] for f in final_state_checks(before, snapshot(self.visible), "repair_caller")["failures"]])

    def test_v1_remains_strict_on_identical_recovered_trace(self):
        self.late_candidate(); self.complete_recovery()
        result = self.audit(contract=LEGACY_EXECUTION_CONTRACT)
        self.assertEqual([f["check"] for f in result["failures"]], ["build_without_confirmed_native_capture"])
        self.assertNotIn("recoveries", result)

    def test_v2_keeps_its_original_resource_and_recovery_requirements(self):
        self.late_candidate(); self.complete_recovery()
        self.truncate_resource("references/repair-basis.md")
        self.truncate_resource("references/workspace-recovery.md")
        result = self.audit(contract=RECOVERY_EXECUTION_CONTRACT)
        self.assertEqual(result["failures"], [])
        self.assertEqual(len(result["recoveries"]), 1)

    def test_normal_build_does_not_require_recovery_resource(self):
        self.workspace("capture"); self.build(); self.clean(); self.workspace("check")
        result = self.audit()
        self.assertEqual(result["failures"], [])
        self.assertNotIn("references/workspace-recovery.md", [r["resource"] for r in result["resource_reads"]])

    def test_candidate_write_requires_complete_split_basis_before_action(self):
        self.truncate_resource("references/repair-basis.md")
        self.call("edit_file", {"path": "Example.java"}, "done",
                  lambda: (self.repo / "src/main/java/Example.java").write_text("class Changed {}"))
        self.load_resource("references/repair-basis.md")  # A late read cannot repair the earlier gate.
        self.workspace("capture"); self.build(); self.clean(); self.workspace("check")
        self.assertIn("required_skill_resource_not_loaded_before_action:references/repair-basis.md", self.checks())

    def candidate_and_verified_cleanup(self):
        self.call("edit_file", {"path": "Example.java"}, "done",
                  lambda: (self.repo / "src/main/java/Example.java").write_text("class Changed {}"))
        self.workspace("capture"); self.build(); self.clean(); self.workspace("check")

    def test_specialist_and_basis_do_not_replace_common_diagnosis_read(self):
        self.events = []
        for name in ("SKILL.md", "references/java-maven-compilation.md", "references/repair-basis.md",
                     "references/repair-and-verification.md", "references/workspace-cleanup.md"):
            self.load_resource(name)
        self.candidate_and_verified_cleanup()
        result = self.audit()
        self.assertEqual(result["workspace_checks"][-1]["result"], "match")
        self.assertEqual([f["check"] for f in result["failures"]],
                         ["required_skill_resource_not_loaded_before_action:references/diagnosis-and-repair.md"])

    def test_truncated_diagnosis_read_still_blocks_candidate(self):
        self.truncate_resource("references/diagnosis-and-repair.md")
        self.candidate_and_verified_cleanup()
        self.assertIn("required_skill_resource_not_loaded_before_action:references/diagnosis-and-repair.md", self.checks())

    def test_late_diagnosis_read_does_not_erase_prior_candidate_violation(self):
        self.truncate_resource("references/diagnosis-and-repair.md")
        self.candidate_and_verified_cleanup()
        original = self.audit()["failures"]
        self.load_resource("references/diagnosis-and-repair.md")
        self.assertEqual(self.audit()["failures"], original)
        self.assertIn("required_skill_resource_not_loaded_before_action:references/diagnosis-and-repair.md", self.checks())

    def test_complete_diagnosis_reread_before_candidate_satisfies_existing_gate(self):
        self.truncate_resource("references/diagnosis-and-repair.md")
        self.load_resource("references/diagnosis-and-repair.md")
        self.candidate_and_verified_cleanup()
        self.assertEqual(self.audit()["failures"], [])
        self.assertEqual(self.audit()["gaps"], [])

    def test_recovery_read_must_be_complete_and_precede_recapture(self):
        self.late_candidate(); self.complete_recovery()
        self.truncate_resource("references/workspace-recovery.md")
        self.load_resource("references/workspace-recovery.md")
        result = self.audit()
        self.assertIn("required_skill_resource_not_loaded_before_action:references/workspace-recovery.md",
                      [f["check"] for f in result["failures"]])
        self.assertEqual(result["recoveries"], [])

    def test_stop_does_not_require_candidate_or_cleanup_branch_reads(self):
        for resource in ("references/repair-basis.md", "references/repair-and-verification.md", "references/workspace-cleanup.md"):
            self.truncate_resource(resource)
        self.assertEqual(self.audit("stop")["failures"], [])

    def test_resource_layout_selects_frozen_package_without_downgrade(self):
        self.assertEqual(execution_contract(self.skill), EXECUTION_CONTRACT)
        declaration = self.skill / "assets/execution-resources.json"
        declaration.write_text('{"contract": "unknown"}')
        with self.assertRaisesRegex(EvalError, "unsupported"):
            execution_contract(self.skill)
        declaration.unlink()
        with self.assertRaisesRegex(EvalError, "declaration missing"):
            execution_contract(self.skill)
        old = self.trial / "legacy-skill"
        write_new(old / "SKILL.md", "# Frozen pre-split Skill\n")
        self.assertEqual(execution_contract(old), RECOVERY_EXECUTION_CONTRACT)

    def test_recapture_and_match_without_rerun_cannot_recover(self):
        self.late_candidate()
        self.workspace("capture", "late"); self.workspace("check", "late")
        self.assertEqual(self.audit()["recoveries"], [])
        self.assertIn("final_capture_has_no_subsequent_build", self.checks())

    def test_polluted_recapture_with_rerun_and_match_cannot_recover(self):
        self.late_candidate()
        write_new(self.repo / "target/forgotten-output", "residue")
        self.complete_recovery()
        self.assertEqual(self.audit()["workspace_checks"][-1]["result"], "match")
        self.assertEqual(self.audit()["recoveries"], [])
        self.assertIn("build_without_confirmed_native_capture", self.checks())

    def test_empty_dir_residue_or_missing_job_file_cannot_be_rebased_away(self):
        self.late_candidate()
        (self.repo / "target/empty-residue").mkdir()
        (self.repo / "target/preexisting.txt").unlink()
        self.complete_recovery()
        self.assertEqual(self.audit()["recoveries"], [])

    def test_different_candidate_is_not_proven_same_state_recovery(self):
        self.late_candidate()
        self.call("edit_file", {"path": "Example.java"}, "done",
                  lambda: (self.repo / "src/main/java/Example.java").write_text("class Another {}"))
        self.complete_recovery()
        self.assertEqual(self.audit()["recoveries"], [])

    def test_missing_prebuild_snapshot_or_trace_gap_blocks_recovery(self):
        self.late_candidate(); self.complete_recovery()
        self.assertEqual(self.audit(trace_gaps=["event_sequence_or_payload_gap"])["recoveries"], [])
        first_build = next(e for e in self.events if e["kind"] == "InvocationToolCallStart" and is_build(e["payload"]))
        first_build["payload"].pop("source_state_sha256")
        self.assertEqual(self.audit()["recoveries"], [])

    def test_final_drift_blocks_recovery(self):
        self.late_candidate(); self.complete_recovery()
        write_new(self.repo / "target/after-match", "residue")
        self.assertEqual(self.audit()["recoveries"], [])
        self.assertIn("workspace_changed_after_cleanup_check", self.checks())

    def test_native_protocol_violation_is_not_recoverable(self):
        self.late_candidate()
        self.call("read_file", {"path": str(self.skill / "SKILL.md")}, "bypassed")
        self.complete_recovery()
        self.assertEqual(self.audit()["recoveries"], [])
        self.assertIn("skill_native_tool_bypassed", self.checks())

    def test_invalid_receipt_is_not_repaired_by_later_clean_receipts(self):
        self.late_candidate()
        self.events[-1]["payload"]["result"] = "unbound mismatch"
        self.complete_recovery()
        self.assertTrue(self.audit()["gaps"])
        self.assertEqual(self.audit()["recoveries"], [])

    def test_candidate_rename_without_reconstruction_is_not_recovery(self):
        self.late_candidate()
        write_new(self.repo / "target/forgotten-output", "residue")
        self.workspace("capture", "new-name", candidate_id="new-candidate")
        self.build(); self.clean()
        self.workspace("check", "new-name", candidate_id="new-candidate")
        self.assertEqual(self.audit()["recoveries"], [])

    def test_cleanup_recovery_does_not_admit_semantic_change_under_stop(self):
        from grading import final_state_checks
        before = snapshot(self.visible)
        self.late_candidate(); self.complete_recovery()
        self.assertEqual(self.audit("stop")["failures"], [])
        checks = final_state_checks(before, snapshot(self.visible), "stop")
        self.assertIn("retained_semantic_candidate_under_ambiguity", [f["check"] for f in checks["failures"]])

    def test_late_capture_does_not_erase_earlier_violation(self):
        self.build(); self.workspace("capture"); self.workspace("check")
        self.assertIn("build_without_confirmed_native_capture", self.checks())

    def test_recapture_cannot_hide_products_from_final_state_guard(self):
        from grading import final_state_checks
        before = snapshot(self.visible)
        self.workspace("capture"); self.build(); self.workspace("capture", "polluted"); self.workspace("check", "polluted")
        self.assertIn("protected_inputs_or_cleanup", [x["check"] for x in final_state_checks(before, snapshot(self.visible), "repair_caller")["failures"]])

    def test_candidate_change_invalidates_capture_even_between_builds(self):
        self.workspace("capture"); self.build(); self.clean()
        self.call("edit_file", {"path": "Example.java"}, "done",
                  lambda: (self.repo / "src/main/java/Example.java").write_text("class Changed {}"))
        self.build()
        self.assertIn("build_without_confirmed_native_capture", self.checks())

    def test_new_capture_after_candidate_change_needs_new_build(self):
        self.workspace("capture"); self.build(); self.clean()
        self.call("edit_file", {"path": "Example.java"}, "done",
                  lambda: (self.repo / "src/main/java/Example.java").write_text("class Changed {}"))
        self.workspace("capture", "two"); self.workspace("check", "two")
        self.assertIn("final_capture_has_no_subsequent_build", self.checks())

    def test_drift_after_match_is_not_accepted(self):
        self.workspace("capture"); self.build(); self.clean(); self.workspace("check")
        write_new(self.repo / "target/late-output", "late")
        self.assertIn("workspace_changed_after_cleanup_check", self.checks())

    def test_latest_incomplete_does_not_reuse_old_match(self):
        self.workspace("capture"); self.build(); self.clean(); self.workspace("check")
        self.workspace("check", candidate_id="wrong")
        self.assertIn("final_native_cleanup_match_unconfirmed", self.checks())

    def test_shell_receipt_is_preserved_without_granting_native_provenance(self):
        self.workspace("capture"); self.build(); self.clean(); self.workspace("check")
        for e in self.events:
            p = e["payload"]
            if e["kind"] == "InvocationToolCallStart" and p.get("tool_name") == "run_skill_script":
                p.update(tool_name="bash", args={"command": "python3 " + str(self.skill / "scripts/workspace_check.py")})
        result = self.audit()
        self.assertEqual([r["result"] for r in result["non_native_workspace_observations"]], ["captured", "match"])
        self.assertIn("skill_native_tool_bypassed", self.checks())
        self.assertIn("final_native_cleanup_match_unconfirmed", self.checks())

    def test_wrong_scope_and_forged_match_are_not_accepted(self):
        self.workspace("capture"); self.build(); self.workspace("check")
        p = self.events[-1]["payload"]
        data = workspace_result(p["result"])
        data.update(result="match", counts={"added": 0, "missing": 0, "changed": 0})
        p["result"] = json.dumps(data) + "\n[exit_code: 0]"
        self.assertTrue(self.audit()["gaps"])
        data["roots"] = [str(self.repo / "src")]
        p["result"] = json.dumps(data) + "\n[exit_code: 0]"
        self.assertTrue(self.audit()["gaps"])

    def test_modified_manifest_rejected_against_original_capture(self):
        self.workspace("capture"); self.build(); self.clean(); self.workspace("check")
        path = self.out / "one.json"
        value = json.loads(path.read_text()); value["candidate_id"] = "different"
        path.write_text(json.dumps(value))
        self.assertTrue(self.audit()["gaps"])

    def test_stop_without_writes_does_not_require_build_or_cleanup(self):
        self.assertEqual(self.audit("stop")["failures"], [])

    def test_raw_file_read_is_protocol_failure_not_missing_content_excuse(self):
        self.call("read_file", {"path": str(self.skill / "SKILL.md")}, read_regular(self.skill / "SKILL.md").decode())
        self.assertIn("skill_native_tool_bypassed", self.checks("stop"))

    def test_truncated_cleanup_guide_cannot_satisfy_prebuild_read(self):
        self.events[-1]["payload"]["result"] = "# 验证副产物清理与校验\n[truncated]"
        self.workspace("capture"); self.build()
        self.assertIn("required_skill_resource_not_loaded_before_action:references/workspace-cleanup.md", self.checks())

    def test_missing_native_load_is_not_registration_success(self):
        self.events[1]["payload"]["result"] = "available skill: ci-self-healing"
        self.assertIn("native_skill_load_unconfirmed", self.checks("stop"))

    def test_result_and_exit_code_must_agree(self):
        for value in ('{"schema":"ci-workspace-check/v1","result":"match"}\n[exit_code: 1]', 'match', '{}\n[exit_code: 0]'):
            with self.assertRaises(ValueError):
                workspace_result(value)

    def test_native_zero_exit_may_be_implicit_but_mismatch_cannot(self):
        self.assertEqual(workspace_result('{"schema":"ci-workspace-check/v1","result":"match"}')["result"], "match")
        with self.assertRaises(ValueError):
            workspace_result('{"schema":"ci-workspace-check/v1","result":"mismatch"}')

    def test_build_detection_ignores_version_queries_and_quoted_examples(self):
        for command in ("mvn --version", "echo 'mvn compile'", "cat pom.xml"):
            self.assertFalse(is_build({"tool_name": "bash", "args": {"command": command}}))
        self.assertTrue(is_build({"tool_name": "bash", "args": {"command": "cd /source && mvn -o compile"}}))


if __name__ == "__main__":
    unittest.main()
