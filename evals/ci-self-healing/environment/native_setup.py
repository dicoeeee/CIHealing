#!/usr/bin/env python3
"""Prepare a new local test environment; no Docker, global changes or model calls.

--warm-cache downloads only the public seed's Maven build dependencies into a
dedicated repository. --build-smoke runs all synthetic fixtures/known variants;
those checks validate the harness, not model repairs.
"""

import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import EvalError, command, read_regular, run_logged, write_json, write_new
from host_sandbox import environment
from native_repair import SETTINGS, doctor, load_config, probe_build
from cases import CASES, materialize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-profile", type=Path)
    parser.add_argument("--endpoint-host", default="configure-before-live.example.invalid")
    parser.add_argument("--java-home", type=Path)
    parser.add_argument("--git", type=Path, help="explicit real Git executable; do not require Xcode dispatch")
    parser.add_argument("--cli", type=Path, help="explicit native CLI executable; defaults to icode on PATH, never falls back to chrys")
    parser.add_argument("--warm-cache", action="store_true")
    parser.add_argument("--build-smoke", action="store_true")
    parser.add_argument("--repository", type=Path, help="reuse an existing dedicated cache read-only; do not combine with --warm-cache")
    args = parser.parse_args()
    if args.repository and args.warm_cache:
        raise EvalError("warmup must not overwrite an existing cache")
    cli = str(args.cli.absolute()) if args.cli else shutil.which("icode")
    if not cli:
        raise EvalError("icode is not installed or on PATH; select an existing CLI using --cli")
    output = args.output.absolute()
    if output.exists() or output.is_symlink() or output != output.resolve() or ROOT / "runs" not in output.parents:
        raise EvalError("output must be a new canonical directory under evaluator runs")
    output.mkdir(parents=True, mode=0o700)
    for name in ("repository", "tmp", "java-home"):
        (output / name).mkdir()
    write_new(output / "settings.xml", SETTINGS)
    config = {"backend": "native", "python": sys.executable, "chrys": cli, "maven": shutil.which("mvn"),
              "java_home": str(args.java_home or Path(command(["/usr/libexec/java_home"]).decode().strip())),
              "maven_repository": str(args.repository.absolute() if args.repository else output / "repository"), "endpoint_host": args.endpoint_host,
              "model_profile": str((args.model_profile or output / "MODEL_PROFILE_NOT_CONFIGURED").absolute()),
              "timeout_seconds": 900, "max_tool_calls": 80}
    if args.git:
        config["git"] = str(args.git.absolute())
    config = load_config(config)
    write_json(output / "runtime.json", config)
    if args.warm_cache:
        write_new(output / "seed/pom.xml", read_regular(ROOT / "environment/cache-pom.xml"))
        write_new(output / "seed/src/main/java/CacheSeed.java", read_regular(ROOT / "environment/CacheSeed.java"))
        result = run_logged([config["maven"], "-B", "-s", str(output / "settings.xml"), "-gs", str(output / "settings.xml"),
                             "-Dmaven.repo.local=" + config["maven_repository"], "clean", "compile"],
                            output / "cache-warmup", 300, env=environment(config, output), cwd=output / "seed")
        if result["status"] != "completed" or result["returncode"] != 0:
            raise EvalError("public Maven cache warmup incomplete; inspect cache-warmup logs")
    report = {"models_called": False, "model_profile_configured": args.model_profile is not None,
              "doctor": doctor(config, output / "doctor"), "build_smoke": {}}
    if args.build_smoke:
        for case_id in CASES:
            base = output / "build-smoke" / case_id
            materialize(base / "visible", case_id, "native-build-smoke", backend="native")
            source = base / "visible/scene/sourcecode"
            baseline = probe_build(config, source, base / "baseline")
            report["build_smoke"][case_id] = {"baseline": baseline["status"]}
            if case_id == "case-003":
                continue
            known = base / "known-variant"
            shutil.copytree(source, known)
            java = known / "src/main/java/example"
            if case_id == "case-001":
                file = java / "Feature.java"
                file.write_text(file.read_text().replace("route(payload, condition, slot)", "route(payload)"))
            else:
                file = java / "Router.java"
                file.write_text(file.read_text().replace("String route(String payload);", "String route(String payload);\n    String route(String payload, String condition, int slot);"))
                file = java / "DefaultRouter.java"
                file.write_text(file.read_text().replace("public final class DefaultRouter implements Router {", 'public final class DefaultRouter implements Router {\n    public String route(String payload, String condition, int slot) { return condition + ":" + slot + ":" + payload; }'))
            report["build_smoke"][case_id]["known_variant"] = probe_build(config, known, base / "variant", case_id)["status"]
    write_json(output / "result.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(s == "passed" for row in report["build_smoke"].values() for s in row.values()) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "incomplete", "error_type": type(exc).__name__, "error": str(exc) if isinstance(exc, EvalError) else "see local logs"}), file=sys.stderr)
        raise SystemExit(2)
