#!/usr/bin/env python3
"""CLI for the first CI Skill regression suite. Live model calls are opt-in."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from common import EvalError
from grading import grade, summarize
from review import review
from runtime import runtime_probe, load_runtime, run_trials
from suite import REPO, prepare


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    create = commands.add_parser("prepare", help="freeze two Skill snapshots and create fresh blind fixtures; no model calls")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--candidate", type=Path, default=REPO / "skills/ci-self-healing")
    baseline = create.add_mutually_exclusive_group()
    baseline.add_argument("--baseline-ref", default="HEAD")
    baseline.add_argument("--baseline-dir", type=Path)
    create.add_argument("--repeats", type=int, default=3)
    create.add_argument("--seed", type=int, default=20260929)
    create.add_argument("--backend", choices=["native", "docker"], default="native")
    create.add_argument("--reviewer", choices=["glm-5.3", "gpt-6-sol"], default="glm-5.3")
    doctor = commands.add_parser("doctor", help="check existing isolated runtime; no model calls, image pulls, or installs")
    doctor.add_argument("--runtime", type=Path, required=True)
    batch = commands.add_parser("test", help="repeatable whole-suite pipeline; defaults to a no-call plan")
    batch.add_argument("--config", type=Path, required=True)
    batch.add_argument("--mode", choices=["single", "compare"])
    batch.add_argument("--preset", choices=["smoke", "regression"])
    batch.add_argument("--candidate", type=Path)
    batch.add_argument("--baseline-ref")
    batch.add_argument("--cases", help="comma-separated debug filter; normally run every registered case")
    batch.add_argument("--resume", type=Path)
    batch.add_argument("--execute-live", action="store_true")
    batch.add_argument("--plan", action="store_true", help="print the plan without creating fixtures or calling models")
    retry = commands.add_parser("review-attempt", help="separate native assessment of an incomplete frozen review; never rerun repair or overwrite a batch")
    retry.add_argument("--source-review", type=Path, required=True)
    retry.add_argument("--output", type=Path, required=True)
    retry.add_argument("--config", type=Path, required=True, help="existing batch config; only its native reviewer configuration is used")
    retry.add_argument("--execute-live", action="store_true")
    for action in ("run", "grade", "review", "summary"):
        sub = commands.add_parser(action)
        sub.add_argument("--suite", type=Path, required=True)
        if action != "summary":
            sub.add_argument("--trial", help="one opaque trial ID; otherwise all prepared trials")
        if action in {"run", "grade"}:
            sub.add_argument("--runtime", type=Path, required=action == "run")
        if action in {"run", "review"}:
            sub.add_argument("--execute-live", action="store_true", help="explicitly permit billable model execution")
        if action == "review":
            sub.add_argument("--timeout", type=int, help="wall-clock seconds; GLM default 600, legacy Sol default 300")
            sub.add_argument("--max-output-tokens", type=int, help="GLM output + reasoning budget; default 16384, maximum 32768; bounded by profile")
            sub.add_argument("--max-packet-bytes", type=int, help="GLM complete evidence byte budget; default 524288, maximum 1048576; never truncates")
            sub.add_argument("--review-profile", type=Path, help="explicit Chrys GLM 5.3 Responses profile; required for GLM review")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in {"run", "review"} and not args.execute_live:
            raise EvalError("live execution requires --execute-live; no model was called")
        if args.command == "review-attempt":
            from batch import load_config
            from review_attempt import review_attempt
            result = review_attempt(args.source_review, args.output, load_config(args.config)["reviewer"], execute=args.execute_live)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "planned" or result.get("verdict") == "pass" else 2
        elif args.command == "test":
            from batch import execute_batch, load_config, make_plan, new_run
            from common import read_json
            config = load_config(args.config)
            if args.resume:
                if any((args.mode, args.preset, args.candidate, args.baseline_ref, args.cases)):
                    raise EvalError("resume uses the frozen selection; do not combine with new-run options")
                plan = read_json(args.resume / "batch.json")["plan"]
            else:
                plan = make_plan(config, args.candidate or REPO / "skills/ci-self-healing", args.mode or "single",
                                 args.preset or "smoke", args.baseline_ref or "HEAD", args.cases.split(",") if args.cases else None)
            if args.plan or not args.execute_live:
                print(json.dumps({"status": "planned", **plan, "execute_live_required": True}, ensure_ascii=False, indent=2))
                return 0
            root = args.resume.absolute() if args.resume else new_run(config, plan)
            print(json.dumps({"batch_directory": str(root)}, ensure_ascii=False), file=sys.stderr, flush=True)
            result = execute_batch(root, config)
            print(json.dumps({"batch_directory": str(root), **result}, ensure_ascii=False, indent=2))
            return result["exit_code"]
        elif args.command == "prepare":
            manifest = prepare(args.output, args.candidate, args.baseline_ref, args.baseline_dir, args.repeats, args.seed, args.reviewer, backend=args.backend)
            result = {"status": "prepared", "suite": str(args.output.absolute()), "trial_count": len(manifest["trials"]), "models_called": False}
        elif args.command == "doctor":
            result = runtime_probe(load_runtime(args.runtime))
        elif args.command == "run":
            result = run_trials(args.suite, args.runtime, args.trial)
        elif args.command == "grade":
            result = grade(args.suite, args.runtime, args.trial)
        elif args.command == "review":
            result = review(args.suite, args.trial, args.timeout, args.review_profile, args.max_output_tokens, args.max_packet_bytes)
        else:
            result = summarize(args.suite)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (EvalError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "incomplete", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
