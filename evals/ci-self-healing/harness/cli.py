"""Local-first evaluation CLI. Planning/checking never invokes a model."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from common import EvalError

from .catalog import Catalog
from .core import ENGINE, REPO, EvaluationError, read_json
from .runner import calibrate, calibration_plan, prepare_calibration, load_runtime, make_plan, report, run


def parser():
    from .calibration import SUITES
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog", help="validate behavior-led scenario coverage; no models")
    check = commands.add_parser("check", help="catalog, framework and Skill script tests; no models")
    check.add_argument("--catalog-only", action="store_true")
    check.add_argument("--suite", choices=("isolation",), help="run controlled workspace isolation probes; no Agent/model")
    check.add_argument("--output", type=Path, help="new sealed output directory for --suite isolation")
    regression = commands.add_parser("regression", help="run local tests and replay the reviewed Java evidence; no models")
    regression.add_argument("--source", type=Path, help="location of the same sealed Java phase2 run")
    regression.add_argument("--runtime", type=Path, help="use the selected Python and offline native/Maven fixtures")
    doctor = commands.add_parser("doctor", help="probe selected runtime and sandbox without a model")
    doctor.add_argument("--runtime", required=True, type=Path)
    for name in ("plan", "run"):
        command = commands.add_parser(name, help="plan or execute a new frozen selection")
        command.add_argument("--candidate", type=Path, default=REPO / "skills/ci-self-healing")
        command.add_argument("--baseline", type=Path, help="old Skill snapshot for paired comparison")
        command.add_argument("--suite", choices=("smoke", "core", "disposition", "delivery", "all"), default="smoke")
        command.add_argument("--cases", help="comma-separated case IDs")
        command.add_argument("--tags", help="comma-separated tags; matches any requested tag")
        command.add_argument("--repeats", type=int, default=1)
        command.add_argument("--max-sessions", type=int, default=16)
        command.add_argument("--wall-seconds", type=int, default=7200)
        command.add_argument("--stage-seconds", type=int, default=900)
        command.add_argument("--concurrency", type=int, default=3)
        if name == "run":
            command.add_argument("--runtime", required=True, type=Path)
            command.add_argument("--output", required=True, type=Path)
            command.add_argument("--execute-live", action="store_true")
    calibration = commands.add_parser("calibrate", help="six fixed-evidence judge calibration samples")
    calibration.add_argument("--suite", choices=SUITES, default="initial-v1")
    calibration.add_argument("--runtime", type=Path, help="required only with --execute-live")
    calibration.add_argument("--output", required=True, type=Path)
    calibration.add_argument("--max-sessions", type=int, default=6)
    calibration.add_argument("--wall-seconds", type=int, default=1800)
    calibration.add_argument("--stage-seconds", type=int, default=900)
    calibration.add_argument("--concurrency", type=int, default=3)
    mode = calibration.add_mutually_exclusive_group()
    mode.add_argument("--execute-live", action="store_true")
    mode.add_argument("--prepare-only", action="store_true", help="freeze reviewable blind prompts and separate labels; no runtime/model")
    gate = commands.add_parser("calibration-gate", help="offline three-part release check of sealed calibration outputs")
    gate.add_argument("--run", required=True, type=Path)
    gate.add_argument("--audit", type=Path, help="source-bound rationale audit; omission produces a pending template")
    gate.add_argument("--output", required=True, type=Path, help="new directory outside the original calibration run")
    gate_report = commands.add_parser("calibration-gate-report", help="verify source, implementation and seal before reading a gate")
    gate_report.add_argument("--gate", required=True, type=Path)
    report = commands.add_parser("report", help="read saved results after checking evidence seals")
    report.add_argument("--run", required=True, type=Path)
    grade = commands.add_parser("grade", help="recheck frozen evidence with no model; preserve old semantic opinion")
    grade.add_argument("--run", required=True, type=Path)
    grade.add_argument("--output", required=True, type=Path)
    grade.add_argument("--check-review-evidence", action="store_true",
                       help="check citations of saved semantic passes against the frozen observations")
    review = commands.add_parser("review", help="one new independent assessment; never rerun execution")
    review.add_argument("--trial", required=True, type=Path)
    review.add_argument("--output", required=True, type=Path)
    review.add_argument("--runtime", required=True, type=Path)
    review.add_argument("--execute-live", action="store_true")
    return result


def _runtime(args, argv):
    runtime = load_runtime(args.runtime)
    selected = Path(runtime["python"])
    if not selected.is_file() or not os.access(selected, os.X_OK):
        raise EvaluationError("selected runtime Python is unavailable")
    if Path(sys.executable).absolute() != selected.absolute():
        if os.environ.get("CI_EVAL_SELECTED_PYTHON") == str(selected):
            raise EvaluationError("selected Python re-exec did not establish its runtime")
        env = dict(os.environ, CI_EVAL_SELECTED_PYTHON=str(selected), PYTHONDONTWRITEBYTECODE="1")
        os.execve(str(selected), [str(selected), "-B", str(ENGINE / "eval.py"), *argv], env)
    from .review_schema import require_validator
    require_validator()
    return runtime


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv)
    try:
        if args.command == "report":
            result = report(args.run)
        elif args.command == "calibration-gate":
            from .calibration_gate import assess
            result = assess(args.run, args.output, audit_path=args.audit)
        elif args.command == "calibration-gate-report":
            from .calibration_gate import read_gate
            result = read_gate(args.gate)
        elif args.command == "grade":
            from .reassessment import grade
            result = grade(args.run, args.output, check_review_evidence=args.check_review_evidence)
        elif args.command == "regression":
            from . import local_regression
            from .review_schema import require_validator
            env = None
            if args.runtime:
                runtime = _runtime(args, argv)
                env = dict(os.environ, CI_EVAL_V2_NATIVE_RUNTIME=str(args.runtime.resolve()),
                           CI_FIXTURE_MAVEN=runtime["maven"], CI_FIXTURE_MAVEN_CACHE=runtime["maven_repository"],
                           CI_FIXTURE_JAVA_HOME=runtime["java_home"])
            require_validator()
            result = local_regression.run(Catalog(), args.source, env)
        elif args.command == "review":
            from .reassessment import review
            result = review(args.trial, args.output, _runtime(args, argv) if args.execute_live else {},
                            execute_live=args.execute_live)
        elif args.command == "doctor":
            from .native import preflight
            result = preflight(_runtime(args, argv))
        elif args.command == "calibrate":
            options = {"suite": args.suite, "maximum_sessions": args.max_sessions,
                       "wall_seconds": args.wall_seconds, "stage_seconds": args.stage_seconds,
                       "concurrency": args.concurrency}
            if args.prepare_only:
                result = prepare_calibration(args.output, **options)
            elif not args.execute_live:
                result = calibration_plan(**options)
            else:
                if args.runtime is None:
                    raise EvaluationError("--runtime is required with calibrate --execute-live")
                result = calibrate(args.output, _runtime(args, argv), **options)
        else:
            catalog = Catalog()
            if args.command == "check" and args.output is not None and args.suite is None:
                raise EvaluationError("check --output requires --suite isolation")
            if args.command in {"catalog", "check"}:
                result = catalog.summary()
                if args.command == "catalog":
                    result["cases"] = [{"id": c["id"], "title": c["title"], "behaviors": c["behavior_ids"],
                                        "criteria": len(c["criteria_ids"]), "tags": c["tags"]} for c in catalog.cases.values()]
                elif args.suite:
                    if args.catalog_only or args.output is None:
                        raise EvaluationError("check --suite isolation requires --output and cannot use --catalog-only")
                    from .workspace_isolation import run_offline
                    result = run_offline(args.output, catalog)
                elif not args.catalog_only:
                    from .local_regression import local_tests
                    from .review_schema import require_validator
                    require_validator()
                    outcomes = local_tests()
                    result.update(tests=outcomes, status="passed" if all(o["returncode"] == 0 for o in outcomes) else "failed")
            else:
                plan = make_plan(catalog, args.candidate, baseline=args.baseline, suite=args.suite,
                                 cases=args.cases.split(",") if args.cases else None,
                                 tags=args.tags.split(",") if args.tags else None, repeats=args.repeats,
                                 maximum_sessions=args.max_sessions, wall_seconds=args.wall_seconds,
                                 concurrency=args.concurrency, stage_seconds=args.stage_seconds)
                if args.command == "plan" or not args.execute_live:
                    result = {"status": "planned", **plan}
                else:
                    result = run(args.output, _runtime(args, argv), plan, catalog=catalog)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result.get("exit_code", {"passed": 0, "planned": 0, "failed": 1}.get(result.get("status"), 2))
    except (EvaluationError, EvalError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        error = str(exc) if isinstance(exc, EvaluationError) else type(exc).__name__
        print(json.dumps({"status": "incomplete", "error": error}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
