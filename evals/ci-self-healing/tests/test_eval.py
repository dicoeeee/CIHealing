"""Offline tests of the evaluator, not evidence of real-model skill compliance."""

import dataclasses
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cases import CASES, SOURCES, materialize, oracle
from chrys_observer import Recorder, headless_result
from common import EvalError, digest, json_bytes, read_checked_stream, read_json, read_regular, run_logged, snapshot, write_json, write_new
from grading import CRITERIA, candidate_head, combined_status, final_state_checks, grade, normalize_trace, summarize
from review import make_packet, review, validate_verdict
from runtime import container_base, docker_probe, load_runtime, mount
from suite import REPO, bundle_hash, load_suite, prepare, skill_from_dir


def trace_rows():
    return [
        {"seq": 1, "kind": "observer.started", "payload": {}},
        {"seq": 2, "kind": "InvocationMessage", "payload": {"text": "repair_admission: allow_local_candidate; evidence: mr_context", "is_final": False}},
        {"seq": 3, "kind": "InvocationToolCallStart", "payload": {"call_id": "call-1", "tool_name": "edit_file", "args": {"path": "Feature.java"}}},
        {"seq": 4, "kind": "InvocationToolCallResult", "payload": {"call_id": "call-1", "result": "done", "changed_paths_since_previous_event": ["scene/sourcecode/src/main/java/example/Feature.java"]}},
        {"seq": 5, "kind": "observer.finished", "payload": {"returncode": 0, "result": {"session_id": "s1", "result": "done"}, "model_receipts": [{"session_id": "s1", "model_id": "glm-5.2"}]}},
    ]


def encode_trace(rows):
    return "\n".join("CI_EVAL_EVENT " + json.dumps(r) for r in rows).encode()


def packet():
    events = normalize_trace(encode_trace(trace_rows()), "glm-5.2")["events"]
    return {"events": events, "files": [{"evidence_id": "F0001"}], "deterministic": {"source_changes": ["Feature.java"]}}


def verdict():
    return {"verdict": "pass", "criteria": [{"id": name, "status": "pass", "evidence": ["E0002", "E0004", "F0001"], "explanation": "Known synthetic evidence supports this criterion."} for name in CRITERIA],
            "temporal": {"admission_seq": 2, "first_candidate_change_seq": 4, "earlier_source_changes": []}, "summary": "synthetic calibration only"}


STOP_REPORT = """# CI 自愈诊断与修复报告

| 字段 | 内容 |
| --- | --- |
| invocation_id | synthetic |
| 请求类型 | 修复 |
| 执行模式 | local_verify |
| 绑定 Profile | 未绑定：Skill 未固定，本次调用未指定 |
| work_dir | /trial/scene |
| output_dir | /trial/output |

| 仓库 | 工作空间绝对路径 | baseline_sha | 候选 fingerprint |
| --- | --- | --- | --- |
| fixture | /trial/scene/sourcecode | fixture-sha | 不适用：未修改 |

## 1. 处理结论

inconclusive；本次结束，未修复。

## 2. 失败与根因

参数数量不匹配，无分类要求。

## 3. 目标行为与修复依据

MR 无法区分两种目标；准入 stop，未编辑。

## 4. 实际修改

未修改，无清理动作。

## 5. 验证结果

未声明或执行检查；未创建候选。

## 6. 剩余问题与交接

缺少目标契约，无协作通道；需要明确参数用途后重新调用。
"""


class EvalTests(unittest.TestCase):
    def test_headless_failure_without_stdout_preserves_failure_boundary(self):
        self.assertEqual(headless_result("", 1), {})
        self.assertEqual(headless_result('{"result":"READY"}', 0), {"result": "READY"})
        for raw in ("", "{}", "[]"):
            with self.assertRaises(EvalError):
                headless_result(raw, 0)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-eval-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def skills(self):
        for label in ("before", "after"):
            folder = self.base / label
            write_new(folder / "SKILL.md", "---\nname: fixture\n---\n# Fixture " + label)
            for name in ("scripts/report_check.py", "assets/analysis-fix.template.md"):
                write_new(folder / name, read_regular(REPO / "skills/ci-self-healing" / name))
        return self.base / "before", self.base / "after"

    def config(self, **overrides):
        directory = self.base / "profiles"
        directory.mkdir(exist_ok=True)
        config = {"image": "example/eval@sha256:" + "a" * 64, "network": "eval-internal",
                  "chrys_config_dir": str(directory), "agent": "EvalCode", "expected_model_id": "glm-5.2"}
        config.update(overrides)
        return config

    def test_prepare_two_versions_three_cases_and_no_answer_mount(self):
        before, after = self.skills()
        out = self.base / "suite"
        result = prepare(out, after, baseline_dir=before, repeats=1)
        self.assertEqual(len(result["trials"]), 6)
        self.assertEqual({(r["case"], r["version"]) for r in result["trials"]}, {(c, v) for c in CASES for v in ("before", "after")})
        for row in result["trials"]:
            visible = out / "trials" / row["id"] / "visible"
            prompt = (visible / "task.txt").read_text()
            self.assertNotIn(row["case"], prompt)
            self.assertNotIn(row["expected"], prompt)
            self.assertFalse((visible / "oracle").exists())
            self.assertFalse((visible / "all_logs.txt").exists())
            self.assertEqual(candidate_head(visible / "scene/sourcecode"), row["baseline_sha"])
        self.assertEqual(load_suite(out), result)
        self.assertEqual(summarize(out)["counts"]["after"]["not_run"], 3)

    def test_prepare_default_is_eighteen_and_rejects_overwrite_and_drift(self):
        before, after = self.skills()
        out = self.base / "suite"
        result = prepare(out, after, baseline_dir=before)
        self.assertEqual(len(result["trials"]), 18)
        with self.assertRaises(EvalError):
            prepare(out, after, baseline_dir=before)
        (out / "skills/after/SKILL.md").write_text("changed")
        with self.assertRaises(EvalError):
            load_suite(out)

    def test_identical_skills_rejected(self):
        before, _ = self.skills()
        with self.assertRaises(EvalError):
            prepare(self.base / "suite", before, baseline_dir=before)

    def test_candidate_checker_cannot_weaken_external_contract(self):
        before, after = self.skills()
        (after / "scripts/report_check.py").write_text("print('always pass')")
        prepare(self.base / "suite", after, baseline_dir=before, repeats=1)
        self.assertEqual(read_regular(self.base / "suite/checker/scripts/report_check.py"), read_regular(ROOT / "contracts/v1/scripts/report_check.py"))

    def test_restricted_frontmatter_stops_before_body_read(self):
        class GuardedStream(io.BytesIO):
            def read(self, *args):
                raise AssertionError("body read must not occur")
        with self.assertRaises(EvalError):
            read_checked_stream(GuardedStream(b"---\nai_access: false\n---\nprivate body"), "private.md")

    def test_runtime_snapshot_excludes_tests_and_human_readme(self):
        before, _ = self.skills()
        write_new(before / "README.md", "---\nai_access: false\n---\nPrivate human notes")
        write_new(before / "tests/answer.txt", "hidden answer")
        self.assertEqual(set(skill_from_dir(before)), {"SKILL.md", "scripts/report_check.py", "assets/analysis-fix.template.md"})

    def test_restricted_reference_and_symlink_fail_closed(self):
        before, _ = self.skills()
        write_new(before / "references/private.md", "---\nai_access: false\n---\nDo not read")
        with self.assertRaises(EvalError):
            skill_from_dir(before)
        (before / "references/private.md").unlink()
        (before / "references/link.md").symlink_to(before / "SKILL.md")
        with self.assertRaises(EvalError):
            skill_from_dir(before)

    def test_tool_trace_complete_identity_and_pairs(self):
        trace = normalize_trace(encode_trace(trace_rows()), "glm-5.2")
        self.assertEqual(trace["gaps"], [])
        self.assertEqual(trace["events"][3]["evidence_id"], "E0004")
        for mutate in (lambda r: r.pop(3), lambda r: r.pop(), lambda r: r[-1]["payload"].update(model_receipts=[])):
            rows = trace_rows()
            mutate(rows)
            self.assertTrue(normalize_trace(encode_trace(rows), "glm-5.2")["gaps"])

    def test_final_answer_alone_and_wrong_model_never_complete_trace(self):
        self.assertTrue(normalize_trace(b'{"result":"I complied"}', "glm-5.2")["gaps"])
        result = normalize_trace(encode_trace(trace_rows()), "different-model")
        self.assertIn("model_or_session_identity_unconfirmed", result["gaps"])

    def test_changed_files_detected_even_after_later_restore(self):
        root = self.base / "visible"
        root.mkdir()
        write_new(root / "Example.java", "original")
        stream = io.StringIO()
        recorder = Recorder(stream, root)
        @dataclasses.dataclass
        class InvocationToolCallResult:
            call_id: str = "a"
            result: str = "done"
        (root / "Example.java").write_text("candidate")
        recorder.observe(InvocationToolCallResult())
        (root / "Example.java").write_text("original")
        recorder.observe(InvocationToolCallResult())
        rows = [json.loads(line[len("CI_EVAL_EVENT "):]) for line in stream.getvalue().splitlines()]
        self.assertEqual([r["payload"]["changed_paths_since_previous_event"] for r in rows], [["Example.java"], ["Example.java"]])

    def test_observer_does_not_capture_hidden_thinking_and_redacts_injected_key(self):
        stream = io.StringIO()
        recorder = Recorder(stream, self.base, secret="synthetic-secret")
        @dataclasses.dataclass
        class AgentThinking:
            text: str = "hidden"
        recorder.observe(AgentThinking())
        self.assertEqual(stream.getvalue(), "")
        recorder.emit("test", {"text": "synthetic-secret"})
        self.assertNotIn("synthetic-secret", stream.getvalue())

    def test_cleanup_and_intervention_content_are_hard_checks(self):
        visible = self.base / "visible"
        materialize(visible, "case-001", "trial-id")
        before = snapshot(visible)
        source = visible / "scene/sourcecode/src/main/java/example/Feature.java"
        source.write_text(SOURCES["Feature.java"].replace("payload, condition, slot", "payload"))
        good = final_state_checks(before, snapshot(visible), "repair_caller")
        self.assertEqual(good["failures"], [])
        write_new(visible / "scene/sourcecode/target/Generated.class", b"bytecode")
        self.assertTrue(final_state_checks(before, snapshot(visible), "repair_caller")["failures"])
        (visible / "scene/sourcecode/target/Generated.class").unlink()
        (visible / "scene/sourcecode/target/preexisting.txt").unlink()
        self.assertTrue(final_state_checks(before, snapshot(visible), "repair_caller")["failures"])

    def test_stop_rejects_retained_semantic_patch_but_no_diff_needs_review(self):
        before = {"scene/sourcecode/src/main/java/F.java": {"kind": "file", "sha256": "a"}}
        after = {"scene/sourcecode/src/main/java/F.java": {"kind": "file", "sha256": "b"}}
        self.assertTrue(final_state_checks(before, after, "stop")["failures"])
        self.assertEqual(combined_status({"status": "awaiting_review"}, None), "incomplete")

    def test_candidate_head_never_follows_refs_outside_fixture(self):
        repo = self.base / "repo"
        write_new(repo / ".git/HEAD", "ref: ../../secret\n")
        with self.assertRaises(EvalError):
            candidate_head(repo)

    def test_fixture_git_ignores_inherited_repository_redirects(self):
        rogue = self.base / "unrelated-git"
        with mock.patch.dict(os.environ, {"GIT_DIR": str(rogue), "GIT_WORK_TREE": str(self.base), "GIT_TEMPLATE_DIR": "/not-a-fixture-template"}):
            baseline = materialize(self.base / "visible", "case-001", "synthetic")
        self.assertFalse(rogue.exists())
        self.assertEqual(candidate_head(self.base / "visible/scene/sourcecode"), baseline)

    def test_runtime_requires_immutable_image_internal_network_and_clean_profiles(self):
        for overrides in ({"image": "example/eval:latest"}, {"network": "host"}, {"timeout_seconds": 0}, {"unexpected": "secret"}):
            cfg = self.config(**overrides)
            path = self.base / (str(len(list(self.base.iterdir()))) + ".json")
            write_json(path, cfg)
            with self.assertRaises(EvalError):
                load_runtime(path)
        path = self.base / "good.json"
        write_json(path, self.config())
        self.assertEqual(load_runtime(path)["max_tool_calls"], 80)

    def test_docker_probe_refuses_non_internal_before_starting_model(self):
        responses = [b"27.0", b'[{"Id":"sha256:image"}]', b'[{"Id":"network", "Internal":false}]']
        with mock.patch("runtime.command", side_effect=responses) as run:
            with self.assertRaises(EvalError):
                docker_probe(self.config())
            self.assertEqual(run.call_count, 3)

    def test_container_arguments_protect_host_and_do_not_expand_shell(self):
        with mock.patch("runtime.os.getuid", return_value=501), mock.patch("runtime.os.getgid", return_value=20):
            args = container_base("image", "none", "ci-eval-test")
        self.assertIn("--read-only", args)
        self.assertIn("--cap-drop=ALL", args)
        self.assertNotIn("--privileged", args)
        self.assertIn("--pull=never", args)
        with self.assertRaises(EvalError):
            mount(self.base / "x,readonly=false", "/trial")

    def test_review_requires_real_references_complete_criteria_and_consistent_verdict(self):
        self.assertEqual(validate_verdict(verdict(), packet())["verdict"], "pass")
        for mutate in (lambda v: v["criteria"].pop(),
                       lambda v: v["criteria"][0].update(evidence=["E9999"]),
                       lambda v: v["criteria"][0].update(status="fail"),
                       lambda v: v["temporal"].update(admission_seq=1),
                       lambda v: v["temporal"].update(admission_seq=3),
                       lambda v: v["temporal"].update(admission_seq=5),
                       lambda v: v["temporal"].update(first_candidate_change_seq=3),
                       lambda v: v["temporal"].update(first_candidate_change_seq=None)):
            value = verdict()
            mutate(value)
            with self.assertRaises(EvalError):
                validate_verdict(value, packet())

    def test_retracted_admission_and_tool_result_are_not_automatic_pass(self):
        for kind, extra in (("InvocationToolCallResult", {"result": "repair_admission"}),
                            ("InvocationMessage", {"presentation": {"attempt_id": "unaccepted"}})):
            evidence = packet()
            evidence["events"][1]["kind"] = kind
            evidence["events"][1]["payload"].update(extra)
            with self.assertRaises(EvalError):
                validate_verdict(verdict(), evidence)

    def test_first_edit_cannot_hide_earlier_unadmitted_source_changes(self):
        evidence = packet()
        evidence["events"][0]["payload"]["changed_paths_since_previous_event"] = ["scene/sourcecode/src/main/java/example/Feature.java"]
        with self.assertRaises(EvalError):
            validate_verdict(verdict(), evidence)

    def test_earlier_independent_experiment_is_not_a_global_stop(self):
        evidence = packet()
        evidence["events"][0].update(kind="InvocationMessage", payload={"text": "experiment_only: inspect independent compiler property"})
        evidence["events"][1]["payload"]["changed_paths_since_previous_event"] = ["scene/sourcecode/src/main/java/example/Probe.java"]
        value = verdict()
        value["temporal"]["earlier_source_changes"] = [{"change_seq": 2, "experiment_admission_seq": 1, "evidence": ["E0001", "E0002"], "explanation": "Synthetic independently admitted and restored experiment."}]
        self.assertEqual(validate_verdict(value, evidence)["verdict"], "pass")

    def test_replay_grade_blind_sol_adapter_and_stale_result_invalidation(self):
        """Synthetic transport replay exercises plumbing, not model capability."""
        before, after = self.skills()
        root = self.base / "suite"
        manifest = prepare(root, after, baseline_dir=before, repeats=1, reviewer_model="gpt-6-sol")
        row = next(r for r in manifest["trials"] if r["case"] == "case-003")
        trial = root / "trials" / row["id"]
        write_json(root / "runtime-binding.json", {"synthetic": True})
        write_json(trial / "before-run.json", snapshot(trial / "visible"))
        write_new(trial / "visible/output/analysis-fix.md", STOP_REPORT)
        write_json(trial / "after-run.json", snapshot(trial / "visible"))
        events = trace_rows()
        events[1]["payload"]["text"] = "repair_admission: stop; 未取得区分性证据。"
        events[3]["payload"]["changed_paths_since_previous_event"] = []
        raw = encode_trace(events)
        write_new(trial / "execution/stdout.log", raw)
        write_json(trial / "run.json", {"status": "completed", "process": {"returncode": 0},
                                        "expected_model_id": "glm-5.2", "stdout_sha256": digest(raw)})
        result = grade(root, trial_id=row["id"])
        self.assertEqual(result[0]["status"], "awaiting_review", read_json(trial / "grade.json"))
        evidence = make_packet(root, row)
        self.assertNotIn("version", evidence)
        self.assertNotIn("model_receipts", json.dumps(evidence))
        answer = verdict()
        answer["temporal"] = {"admission_seq": None, "first_candidate_change_seq": None, "earlier_source_changes": []}

        def fake_codex(argv, directory, timeout, env=None, stdin=None):
            self.assertEqual(argv[argv.index("--model") + 1], "gpt-6-sol")
            self.assertIn("--ignore-user-config", argv)
            self.assertNotIn("resume", argv)
            result_file = Path(argv[argv.index("--output-last-message") + 1])
            write_json(result_file, answer)
            write_new(Path(directory) / "stdout.log", '{"type":"turn.completed"}\n')
            return {"status": "completed", "returncode": 0}

        with mock.patch("review.run_logged", side_effect=fake_codex):
            reviewed = review(root, trial_id=row["id"])
        self.assertEqual(reviewed[0]["status"], "validated")
        status = next(r for r in summarize(root)["trials"] if r["id"] == row["id"])
        self.assertEqual(status["status"], "passed")
        grade_path = trial / "grade.json"
        original_grade = read_json(grade_path)
        missing_binding = dict(original_grade)
        missing_binding.pop("bindings")
        grade_path.write_bytes(json_bytes(missing_binding))
        invalid = next(r for r in summarize(root)["trials"] if r["id"] == row["id"])
        self.assertEqual(invalid["status"], "incomplete")
        grade_path.write_bytes(json_bytes(original_grade))
        (trial / "visible/scene/sourcecode/job-input.txt").write_text("drift")
        status = next(r for r in summarize(root)["trials"] if r["id"] == row["id"])
        self.assertEqual(status["status"], "incomplete")
        with self.assertRaises(EvalError):
            make_packet(root, row)

    def test_model_review_never_overrides_deterministic_failure_or_gap(self):
        passed = {"status": "validated", "verdict": verdict()}
        self.assertEqual(combined_status({"status": "failed"}, passed), "failed")
        self.assertEqual(combined_status({"status": "incomplete"}, passed), "incomplete")
        self.assertEqual(combined_status({"status": "awaiting_review"}, passed), "passed")

    def test_glm_review_binds_profile_and_uses_tool_free_worker(self):
        before, after = self.skills()
        root = self.base / "glm-suite"
        manifest = prepare(root, after, baseline_dir=before, repeats=1)
        self.assertEqual(manifest["reviewer_model"], "glm-5.3")
        row = manifest["trials"][0]
        trial = root / "trials" / row["id"]
        write_json(trial / "grade.json", {"status": "awaiting_review"})
        profile = {"model_id": "glm-fixture", "profile_sha256": "safe-config-hash", "max_output_tokens": 8192}
        from review_responses import model_receipt, request_body
        from review_limits import encode_request, input_limits
        identity = model_receipt(profile)
        def fake_worker(argv, directory, timeout, env=None, stdin=None):
            self.assertIn("review_responses.py", argv[2])
            self.assertNotIn("codex", argv)
            body = json.loads(stdin)
            self.assertEqual(set(body), {"instructions", "schema", "packet"})
            self.assertNotIn("safe-config-hash", stdin)
            self.assertEqual(argv[argv.index("--max-output-tokens") + 1], "16384")
            self.assertEqual(argv[argv.index("--max-packet-bytes") + 1], "524288")
            self.assertEqual(timeout, 605)
            write_json(Path(directory) / "stdout.log", {**identity, "status": "validated", "reported_model_id": "glm-fixture", "verdict": verdict(),
                       "input_limits": input_limits(), "diagnostics": {"packet_sha256": digest(json_bytes(body["packet"])),
                       "request_sha256": digest(encode_request(request_body(profile, body)))}})
            return {"status": "completed", "returncode": 0}
        with self.assertRaises(EvalError):
            review(root, row["id"])
        with mock.patch("review_responses.load_profile", return_value=profile), mock.patch("review.make_packet", return_value=packet()), mock.patch("review.run_logged", side_effect=fake_worker):
            result = review(root, row["id"], review_profile=self.base / "explicit-profile.yaml")
        self.assertEqual(result[0]["status"], "validated")
        self.assertEqual(read_json(trial / "review.json")["requested_model"], "glm-5.3")
        binding = read_json(root / "reviewer-binding.json")
        self.assertEqual(binding["timeout_seconds"], 600)
        self.assertEqual(binding["input_limits"], input_limits())
        with mock.patch("review_responses.load_profile", return_value=profile):
            with self.assertRaisesRegex(EvalError, "configuration changed"):
                review(root, row["id"], timeout=601, review_profile=self.base / "explicit-profile.yaml")
            with self.assertRaisesRegex(EvalError, "configuration changed"):
                review(root, row["id"], max_packet_bytes=262144, review_profile=self.base / "explicit-profile.yaml")
        with mock.patch("review_responses.load_profile", return_value={**profile, "model_id": "different"}):
            with self.assertRaises(EvalError):
                review(root, row["id"], review_profile=self.base / "explicit-profile.yaml")

    def test_logged_process_timeout_and_literal_stdin(self):
        text = '$(touch SHOULD_NOT_EXIST) `echo bad` $HOME'
        result = run_logged([sys.executable, "-c", "import sys; print(sys.stdin.read())"], self.base / "literal", 2, stdin=text)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual((self.base / "literal/stdout.log").read_text().strip(), text)
        result = run_logged([sys.executable, "-c", "import time; time.sleep(10)"], self.base / "timeout", 0.1)
        self.assertEqual(result["status"], "timeout")

    def test_glm_failed_worker_diagnostics_are_preserved_not_promoted(self):
        before, after = self.skills()
        root = self.base / "failed-review-suite"
        manifest = prepare(root, after, baseline_dir=before, repeats=1)
        row = manifest["trials"][0]
        trial = root / "trials" / row["id"]
        write_json(trial / "grade.json", {"status": "awaiting_review"})
        profile = {"model_id": "glm-fixture", "profile_sha256": "fixture", "max_output_tokens": 16384}
        def failed_worker(argv, directory, timeout, env=None, stdin=None):
            write_json(Path(directory) / "stdout.log", {"status": "incomplete", "error": {"code": "schema_invalid"},
                                                        "diagnostics": {"stage": "json_schema", "schema_error": {"path": ["criteria"], "rule": "type"}}})
            return {"status": "completed", "returncode": 2}
        with mock.patch("review_responses.load_profile", return_value=profile), mock.patch("review.make_packet", return_value=packet()), \
                mock.patch("review.run_logged", side_effect=failed_worker):
            rows = review(root, row["id"], review_profile=self.base / "profile.yaml")
        saved = read_json(trial / "review.json")
        self.assertEqual(rows[0]["status"], "incomplete")
        self.assertEqual(saved["transport_error"]["code"], "schema_invalid")
        self.assertEqual(saved["transport_diagnostics"]["schema_error"]["path"], ["criteria"])
        self.assertNotIn("verdict", saved)
        self.assertFalse((trial / "review-verdict.json").exists())

    def test_oversized_review_does_not_bind_or_start_a_worker(self):
        before, after = self.skills()
        root = self.base / "oversized-review-suite"
        row = prepare(root, after, baseline_dir=before, repeats=1)["trials"][0]
        trial = root / "trials" / row["id"]
        write_json(trial / "grade.json", {"status": "awaiting_review"})
        profile = {"model_id": "glm-fixture", "profile_sha256": "fixture", "max_output_tokens": 16384}
        with mock.patch("review_responses.load_profile", return_value=profile), mock.patch("review.make_packet", return_value=packet()), \
                mock.patch("review.run_logged") as worker:
            with self.assertRaisesRegex(EvalError, "packet too large"):
                review(root, row["id"], review_profile=self.base / "profile.yaml", max_packet_bytes=1)
        worker.assert_not_called()
        self.assertFalse((root / "reviewer-binding.json").exists())
        self.assertFalse((trial / "review-input").exists())

    def test_make_packet_keeps_all_files_and_events_above_old_budget(self):
        root = self.base / "packet-unit"
        row = {"id": "trial-111111111111", "case": "case-002", "expected": "extend_contract"}
        trial = root / "trials" / row["id"]
        text = "完整证据\n" * 23000
        write_new(trial / "visible/scene/evidence.txt", text)
        events = [{"seq": 1, "evidence_id": "E0001", "kind": "InvocationMessage", "payload": {"text": "record"}}]
        write_json(trial / "grade.json", {"trace": {"events": events}, "status": "awaiting_review"})
        with mock.patch("review.validate_grade_binding"):
            result = make_packet(root, row)
            self.assertEqual(result["events"], events)
            self.assertEqual(result["files"][0]["text"], text)
            with self.assertRaises(EvalError):
                make_packet(root, row, 256 * 1024)

    def test_process_progress_reaches_disk_before_exit(self):
        directory = self.base / "progress"
        script = ("import pathlib,sys,time; print('progress',flush=True); "
                  "p=pathlib.Path(sys.argv[1]); deadline=time.monotonic()+2\n"
                  "while time.monotonic()<deadline:\n"
                  " if p.read_text().startswith('progress'): sys.exit(0)\n"
                  " time.sleep(0.01)\n"
                  "sys.exit(3)")
        result = run_logged([sys.executable, "-c", script, str(directory / "stdout.log")], directory, 3)
        self.assertEqual(result["returncode"], 0)

    def test_process_output_is_bounded_and_never_reported_complete(self):
        result = run_logged([sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"], self.base / "output-limit", 3, limit=128)
        self.assertEqual(result["status"], "output_limit")
        self.assertEqual((self.base / "output-limit/stdout.log").stat().st_size, 128)

    def test_selector_setup_failure_reaps_the_already_started_process(self):
        captured = []
        popen = subprocess.Popen

        def start(*args, **kwargs):
            process = popen(*args, **kwargs)
            captured.append(process)
            return process

        with mock.patch("common.subprocess.Popen", side_effect=start), mock.patch("common.selectors.DefaultSelector", side_effect=OSError("synthetic FD exhaustion")):
            with self.assertRaises(OSError):
                run_logged([sys.executable, "-c", "import time; time.sleep(10)"], self.base / "selector-error", 1)
        self.assertIsNotNone(captured[0].poll())
        self.assertTrue(captured[0].stdout.closed)
        self.assertTrue(captured[0].stderr.closed)

    def test_live_execution_needs_explicit_flag_before_loading_credentials(self):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "eval.py"), "run", "--suite", "/missing", "--runtime", "/missing"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--execute-live", result.stderr)


@unittest.skipUnless(shutil.which("javac") and shutil.which("java"), "JDK required for real compilation fixtures")
class JavaFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-eval-java-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def compile(self, root, probe=None):
        classes = root / "classes"
        classes.mkdir(exist_ok=True)
        files = sorted(str(p) for p in (root / "visible/scene/sourcecode/src").rglob("*.java"))
        if probe:
            write_new(root / "ContractProbe.java", probe)
            files.append(str(root / "ContractProbe.java"))
        return subprocess.run(["javac", "-XDrawDiagnostics", "--release", "17", "-proc:none", "-d", str(classes), *files], capture_output=True, text=True, timeout=30)

    def test_all_original_fixtures_really_fail_at_the_same_contract_mismatch(self):
        for case in CASES:
            root = self.base / case
            materialize(root / "visible", case, "invocation")
            result = self.compile(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Feature.java", result.stderr)
            self.assertIn("compiler.err.cant.apply.symbol", result.stderr)

    def test_both_supported_directions_pass_independent_behavior_oracles(self):
        for case in ("case-001", "case-002"):
            root = self.base / case
            materialize(root / "visible", case, "invocation")
            sources = root / "visible/scene/sourcecode/src/main/java/example"
            if case == "case-001":
                (sources / "Feature.java").write_text(SOURCES["Feature.java"].replace("payload, condition, slot", "payload"))
            else:
                (sources / "Router.java").write_text(SOURCES["Router.java"].replace("\n}", "\n    String route(String payload, String condition, int slot);\n}"))
                (sources / "DefaultRouter.java").write_text(SOURCES["DefaultRouter.java"].replace("\n}", '\n    public String route(String payload, String condition, int slot) { return condition + ":" + slot + ":" + payload; }\n}'))
            result = self.compile(root, oracle(case))
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(["java", "-cp", str(root / "classes"), "ContractProbe"], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_green_compile_wrong_direction_fails_behavior_oracle(self):
        root = self.base / "wrong"
        materialize(root / "visible", "case-002", "invocation")
        source = root / "visible/scene/sourcecode/src/main/java/example/Feature.java"
        source.write_text(SOURCES["Feature.java"].replace("payload, condition, slot", "payload"))
        self.assertEqual(self.compile(root).returncode, 0)
        self.assertNotEqual(self.compile(root, oracle("case-002")).returncode, 0)

    def test_accepting_but_ignoring_new_inputs_compiles_and_fails_behavior(self):
        root = self.base / "ignored-inputs"
        materialize(root / "visible", "case-002", "invocation")
        sources = root / "visible/scene/sourcecode/src/main/java/example"
        (sources / "Router.java").write_text(SOURCES["Router.java"].replace("\n}", "\n    String route(String payload, String condition, int slot);\n}"))
        (sources / "DefaultRouter.java").write_text(SOURCES["DefaultRouter.java"].replace("\n}", '\n    public String route(String payload, String condition, int slot) { return route(payload); }\n}'))
        result = self.compile(root, oracle("case-002"))
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(["java", "-cp", str(root / "classes"), "ContractProbe"], capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)

    def test_signature_only_green_candidate_does_not_satisfy_ambiguous_stop_case(self):
        root = self.base / "ambiguous-signature"
        materialize(root / "visible", "case-003", "invocation")
        before = snapshot(root / "visible")
        sources = root / "visible/scene/sourcecode/src/main/java/example"
        for name in ("Router.java", "DefaultRouter.java"):
            (sources / name).write_text(SOURCES[name].replace("route(String payload)", "route(String payload, String condition, int slot)"))
        self.assertEqual(self.compile(root).returncode, 0)
        checks = final_state_checks(before, snapshot(root / "visible"), "stop")
        self.assertIn("retained_semantic_candidate_under_ambiguity", [f["check"] for f in checks["failures"]])


if __name__ == "__main__":
    unittest.main()
