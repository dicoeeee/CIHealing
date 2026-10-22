"""Prepare blind, repeatable before/after trials without executing a model."""

from pathlib import Path
import random
import re
import subprocess
import uuid

from repair_support import delivery_contract, MANIFEST_DELIVERY_CONTRACTS
from cases import CASES, git_environment, git_metadata, materialize
from common import EvalError, command, digest, json_bytes, markdown_allowed, read_checked_stream, read_json, read_regular, snapshot, write_json, write_new


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
SCHEMA = "ci-skill-eval-v1"
LEGACY_CHECKER_FILES = ("scripts/report_check.py", "assets/analysis-fix.template.md")
MANIFEST_CHECKER_FILE = "scripts/file_change_check.py"


def runtime_path(name):
    path = Path(name)
    return name == "SKILL.md" or (path.parts[0] in {"references", "scripts", "agents", "assets"}
                                  and "__pycache__" not in path.parts and path.suffix != ".pyc"
                                  and path.name.lower() != "readme.md")


def skill_from_dir(source):
    source = Path(source).resolve()
    result = {}
    for path in sorted(source.rglob("*")):
        name = path.relative_to(source).as_posix()
        if not runtime_path(name):
            continue
        if path.is_symlink():
            raise EvalError("Skill snapshot cannot contain symlinks: " + name)
        if path.is_file():
            result[name] = read_regular(path)
    return validate_skill(result)


def skill_from_ref(ref):
    # Treat ref as one revision argument, never an option or a shell fragment.
    sha = command(["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"], cwd=REPO, env=git_environment()).decode().strip()
    prefix = "skills/ci-self-healing/"
    entries = command(["git", "ls-tree", "-rz", sha, "--", prefix], cwd=REPO, env=git_environment())
    result = {}
    for raw in entries.split(b"\0"):
        if not raw:
            continue
        meta, raw_path = raw.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        name = raw_path.decode()[len(prefix):]
        if not runtime_path(name):
            continue
        if kind != "blob" or mode == "120000":
            raise EvalError("unsupported Skill object: " + name)
        process = subprocess.Popen(["git", "cat-file", "blob", oid], cwd=REPO, env=git_environment(), stdout=subprocess.PIPE)
        try:
            result[name] = read_checked_stream(process.stdout, name)
            if process.wait(timeout=30) != 0:
                raise EvalError("failed to read Skill blob")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
    return validate_skill(result), sha


def validate_skill(files):
    if "SKILL.md" not in files:
        raise EvalError("SKILL.md missing from snapshot")
    for name, data in files.items():
        if Path(name).suffix.lower() == ".md":
            markdown_allowed(data, name)
    return files


def bundle_hash(files):
    return digest(json_bytes({name: digest(data) for name, data in sorted(files.items())}))


def prepare(output, candidate, baseline_ref="HEAD", baseline_dir=None, repeats=3, seed=20260929, reviewer_model="glm-5.3",
            mode="compare", case_ids=None, backend="docker"):
    if backend not in {"native", "docker"}:
        raise EvalError("unknown execution backend")
    if reviewer_model not in {"glm-5.3", "gpt-6-sol"}:
        raise EvalError("unsupported reviewer model")
    if not 1 <= repeats <= 10:
        raise EvalError("repeats must be between 1 and 10")
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise EvalError("output must be a new directory")
    after = skill_from_dir(candidate)
    if mode not in {"single", "compare"}:
        raise EvalError("mode must be single or compare")
    selected = list(CASES) if case_ids is None else list(case_ids)
    if not selected or len(set(selected)) != len(selected) or any(c not in CASES for c in selected):
        raise EvalError("unknown, empty or duplicate case selection")
    revision = None
    bundles = {"candidate": after}
    if mode == "compare":
        before, revision = (skill_from_dir(baseline_dir), None) if baseline_dir else skill_from_ref(baseline_ref)
        if bundle_hash(before) == bundle_hash(after):
            raise EvalError("baseline and candidate snapshots are identical")
        bundles = {"before": before, "after": after}
    output.mkdir(parents=True, mode=0o700)
    for label, files in bundles.items():
        for name, data in files.items():
            write_new(output / "skills" / label / name, data)
    # Versioned evaluation contract, not a checker automatically taken from the
    # proposed Skill. Contract migrations must be reviewed as evaluator changes.
    deliveries = {label: delivery_contract(output / "skills" / label) for label in bundles}
    checker = {}
    for name in LEGACY_CHECKER_FILES:
        checker[name] = read_regular(ROOT / "contracts/v1" / name)
        write_new(output / "checker" / name, checker[name])
    if MANIFEST_DELIVERY_CONTRACTS.intersection(deliveries.values()):
        checker[MANIFEST_CHECKER_FILE] = read_regular(ROOT / "contracts/file-change-v1" / MANIFEST_CHECKER_FILE)
        write_new(output / "checker" / MANIFEST_CHECKER_FILE, checker[MANIFEST_CHECKER_FILE])
    jobs = [(case, label, index + 1) for case in selected for label in bundles for index in range(repeats)]
    random.Random(seed).shuffle(jobs)
    records = []
    for case, label, repeat in jobs:
        trial_id = "trial-" + uuid.uuid4().hex[:12]
        trial = output / "trials" / trial_id
        baseline = materialize(trial / "visible", case, trial_id, backend)
        record = {"id": trial_id, "case": case, "version": label, "repeat": repeat,
                  "expected": CASES[case]["expected"], "baseline_sha": baseline,
                  "skill_sha256": bundle_hash(bundles[label])}
        write_json(trial / "initial.json", snapshot(trial / "visible"))
        write_json(trial / "git-initial.json", git_metadata(trial / "visible/scene/sourcecode"))
        records.append(record)
    manifest = {"schema": SCHEMA, "baseline_ref": revision, "seed": seed, "repeats": repeats,
                "mode": mode, "backend": backend, "case_definitions": {c: CASES[c] for c in selected},
                "case_implementation_sha256": digest(read_regular(ROOT / "cases.py")),
                "requested_model_profile": "huoshan-glm5.2", "reviewer_model": reviewer_model,
                "skills": {label: bundle_hash(files) for label, files in bundles.items()},
                "report_contract": "v1", "checker_sha256": bundle_hash(checker),
                "trials": records}
    if MANIFEST_DELIVERY_CONTRACTS.intersection(deliveries.values()):
        manifest.update(delivery_contracts=deliveries, checker_files=list(checker))
    write_json(output / "suite.json", manifest)
    return manifest


def load_suite(root):
    root = Path(root).absolute()
    manifest = read_json(root / "suite.json")
    if manifest.get("schema") != SCHEMA:
        raise EvalError("unsupported suite schema")
    if manifest.get("reviewer_model") not in {"glm-5.3", "gpt-6-sol"}:
        raise EvalError("unsupported reviewer model")
    if "case_definitions" in manifest:
        if (manifest["case_definitions"] != {c: CASES.get(c) for c in manifest["case_definitions"]}
                or manifest.get("case_implementation_sha256") != digest(read_regular(ROOT / "cases.py"))):
            raise EvalError("case contract or implementation changed after preparation")
    ids = []
    for row in manifest["trials"]:
        if not re.fullmatch(r"trial-[0-9a-f]{12}", row["id"]) or row["case"] not in CASES or row["version"] not in {"before", "after", "candidate"}:
            raise EvalError("invalid trial identity")
        if row["expected"] != CASES[row["case"]]["expected"] or row["skill_sha256"] != manifest["skills"][row["version"]]:
            raise EvalError("trial contract or snapshot binding changed")
        ids.append(row["id"])
    if len(set(ids)) != len(ids):
        raise EvalError("duplicate trial identity")
    for label, wanted in manifest["skills"].items():
        if label not in {"before", "after", "candidate"} or bundle_hash(skill_from_dir(root / "skills" / label)) != wanted:
            raise EvalError("Skill snapshot changed after preparation")
    checker_files = manifest.get("checker_files", list(LEGACY_CHECKER_FILES))
    if "delivery_contracts" in manifest:
        actual = {label: delivery_contract(root / "skills" / label) for label in manifest["skills"]}
        if manifest["delivery_contracts"] != actual:
            raise EvalError("frozen Skill delivery contract changed")
        expected = [*LEGACY_CHECKER_FILES, MANIFEST_CHECKER_FILE]
        if not MANIFEST_DELIVERY_CONTRACTS.intersection(actual.values()) or checker_files != expected:
            raise EvalError("invalid manifest checker contract")
    elif checker_files != list(LEGACY_CHECKER_FILES):
        raise EvalError("unsupported checker files")
    checker = {name: read_regular(root / "checker" / name) for name in checker_files}
    if bundle_hash(checker) != manifest["checker_sha256"]:
        raise EvalError("fixed report checker changed")
    return manifest


def select_trials(manifest, trial_id=None):
    rows = [row for row in manifest["trials"] if trial_id is None or row["id"] == trial_id]
    if not rows:
        raise EvalError("unknown trial")
    return rows
