#!/usr/bin/env python3
"""Environment-only probes. A READY task is not a CI repair evaluation."""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cases import materialize
from common import read_regular, write_json, write_new
from grading import normalize_trace
from runtime import container_base, docker_probe, load_runtime, mount, probe_build, run_container
from suite import REPO, skill_from_dir
from repair_support import ADAPTER_FILES, SESSION_MOUNT, SKILL_MOUNT, budget_notice, session_receipt
from manage import CONTEXT, LOCAL_TOKEN


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--chrys-live", action="store_true")
    mode.add_argument("--network", action="store_true")
    args = parser.parse_args()
    os.environ["DOCKER_CONTEXT"] = CONTEXT
    os.environ["TMPDIR"] = str(args.runtime.resolve().parent / "tmp")
    os.environ["CHRYS_EVAL_API_KEY"] = LOCAL_TOKEN
    # tempfile may already have cached its choice when modules were imported.
    import tempfile
    tempfile.tempdir = os.environ["TMPDIR"]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = load_runtime(args.runtime)
    report = {"scope": "environment smoke only; no self-healing trial", "doctor": docker_probe(config)}
    if args.network:
        probe = output / "network_probe.py"
        write_new(probe, read_regular(Path(__file__).parent / "network_probe.py"))
        name = "ci-eval-network-smoke"
        argv = [*container_base(config["image"], config["network"], name), *mount(probe, "/network_probe.py"),
                "--entrypoint", "python3", config["image"], "/network_probe.py", config["expected_model_id"]]
        report["process"] = run_container(argv, output / "execution", 45, name)
        report["checks"] = json.loads(read_regular(output / "execution/stdout.log"))
        write_json(output / "result.json", report)
        print(json.dumps(report["checks"]))
        return 0 if report["process"]["returncode"] == 0 and all(report["checks"].values()) else 1
    elif not args.chrys_live:
        visible = output / "fixture"
        materialize(visible, "case-001", "environment-probe")
        source = visible / "scene/sourcecode"
        report["baseline"] = probe_build(config, source, output / "baseline")
        # Known synthetic fixture variant checks the build/oracle plumbing, not an Agent repair.
        repaired = output / "known-fixture-variant"
        shutil.copytree(source, repaired)
        feature = repaired / "src/main/java/example/Feature.java"
        feature.write_bytes(feature.read_bytes().replace(b"route(payload, condition, slot)", b"route(payload)"))
        report["known_fixture_variant"] = probe_build(config, repaired, output / "variant", "case-001")
    else:
        visible = output / "visible"
        (visible / "scene/sourcecode").mkdir(parents=True)
        write_new(visible / "scene/sourcecode/probe.txt", "READY\n")
        write_new(visible / "task.txt", "This is an environment connectivity and tool-event smoke check, not a CI repair task. Read /trial/scene/sourcecode/probe.txt with read_file once, then reply with exactly its word. Do not modify files or use other tools.\n")
        for name in ADAPTER_FILES:
            write_new(output / "adapter" / name, read_regular(ROOT / name))
        for name, data in skill_from_dir(REPO / "skills/ci-self-healing").items():
            write_new(output / "skill" / name, data)
        name = "ci-eval-environment-smoke"
        preflight_name = name + "-preflight"
        preflight = run_container([*container_base(config["image"], "none", preflight_name),
                                  *mount(output / "skill", SKILL_MOUNT), *mount(output / "adapter", "/harness"),
                                  *mount(config["chrys_config_dir"], "/config"), "--entrypoint", "python3", config["image"],
                                  "-B", "/harness/chrys_preflight.py", "--profiles", "/config", "--agent", config["agent"]],
                                 output / "preflight", 45, preflight_name)
        if preflight["status"] != "completed" or preflight["returncode"] != 0:
            write_json(output / "result.json", {"status": "preflight_failed", "process": preflight})
            return 2
        (output / "native-session").mkdir(mode=0o700)
        write_new(output / "adapter/task.txt", read_regular(visible / "task.txt").decode() + budget_notice(180))
        argv = [*container_base(config["image"], config["network"], name),
                *mount(visible, "/trial", True), *mount(output / "adapter", "/harness"),
                *mount(output / "skill", SKILL_MOUNT), *mount(config["chrys_config_dir"], "/config"),
                *mount(output / "native-session", SESSION_MOUNT, True),
                "--env", "CHRYS_EVAL_API_KEY", "--entrypoint", "python3", config["image"],
                "-B", "/harness/chrys_observer.py", "--agent", config["agent"], "--model", "huoshan-glm5.2", "--max-tools", "4"]
        try:
            report["process"] = run_container(argv, output / "execution", 180, name)
        finally:
            write_json(output / "session-receipt.json", session_receipt(output / "native-session"))
        report["trace"] = normalize_trace(read_regular(output / "execution/stdout.log"), config["expected_model_id"])
        events = report["trace"]["events"]
        finished = [e["payload"] for e in events if e["kind"] == "observer.finished"]
        calls = [e["payload"] for e in events if e["kind"] == "InvocationToolCallStart"]
        report["ready_response"] = len(finished) == 1 and finished[0].get("result", {}).get("result", "").strip() == "READY"
        report["read_tool_observed"] = any(call.get("tool_name") == "read_file" for call in calls)
    write_json(output / "result.json", report)
    if args.chrys_live:
        print(json.dumps({"scope": report["scope"], "process": report["process"],
                          "trace_gaps": report["trace"]["gaps"], "ready_response": report["ready_response"],
                          "read_tool_observed": report["read_tool_observed"], "result": str(output / "result.json")}, ensure_ascii=False))
        return 0 if report["process"]["returncode"] == 0 and not report["trace"]["gaps"] and report["ready_response"] and report["read_tool_observed"] else 1
    else:
        print(json.dumps({"scope": report["scope"], "baseline": report["baseline"]["status"],
                          "known_fixture_variant": report["known_fixture_variant"]["status"], "result": str(output / "result.json")}, ensure_ascii=False))
        return 0 if report["baseline"]["status"] == report["known_fixture_variant"]["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
