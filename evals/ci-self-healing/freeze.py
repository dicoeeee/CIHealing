"""Freeze current source using an existing approved environment; never run models.

Only runtime source, tests, public fixtures and a dedicated Maven cache are
copied. Model profiles remain external references, never copied credentials.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from batch import engine_digest, load_config
from common import EvalError, command, digest, json_bytes, read_json, read_regular, snapshot, write_json, write_new
from runtime import load_runtime
from suite import REPO, ROOT, bundle_hash, skill_from_dir


def freeze(previous, output, *, repair_timeout_seconds=900, max_parallel_trials=3):
    previous, output = Path(previous).absolute(), Path(output).absolute()
    if (type(repair_timeout_seconds) is not int or not 1 <= repair_timeout_seconds <= 1800
            or type(max_parallel_trials) is not int or not 1 <= max_parallel_trials <= 3):
        raise EvalError("invalid freeze repair timeout or parallel trial limit")
    if (output.exists() or output.is_symlink() or output != output.resolve()
            or ROOT / "runs" not in output.parents or previous == output or previous in output.parents):
        raise EvalError("freeze output must be a new canonical directory under runs, outside prior freezes")
    config = load_config(previous / "snapshot/config/batch.json")
    runtime = load_runtime(config["runtime"])
    if config["backend"] != "native" or runtime["backend"] != "native":
        raise EvalError("only the selected installed native CLI environment is supported")
    old = read_json(previous / "freeze-manifest.json")
    # Check the environment source but never read private model profile bodies.
    cache = snapshot(runtime["maven_repository"], skip_git=False)
    if digest(json_bytes(cache)) != old["maven_cache_sha256"]:
        raise EvalError("previous dedicated cache changed")
    if any(v["kind"] not in {"file", "directory"} for v in cache.values()):
        raise EvalError("cache contains unsupported file types")
    paths = [p for p in ROOT.rglob("*") if p.is_file() and "runs" not in p.relative_to(ROOT).parts
             and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name.lower() != "readme.md"]
    files = {p.relative_to(REPO).as_posix(): read_regular(p) for p in paths}
    skill = skill_from_dir(REPO / "skills/ci-self-healing")
    files.update({"skills/ci-self-healing/" + n: b for n, b in skill.items()})
    files.update({p.relative_to(REPO).as_posix(): read_regular(p) for p in (REPO / "skills/ci-self-healing/tests").glob("*.py")})
    output.mkdir(parents=True, mode=0o700)
    frozen = output / "snapshot"
    for name, data in files.items():
        write_new(frozen / name, data)
    cache_target = frozen / "evals/ci-self-healing/runs/maven-cache"
    shutil.copytree(runtime["maven_repository"], cache_target)
    if snapshot(cache_target, skip_git=False) != cache:
        raise EvalError("cache copy differs")
    runtime["maven_repository"] = str(cache_target)
    runtime["timeout_seconds"] = repair_timeout_seconds
    config.update(runtime=str(frozen / "config/runtime.json"), output_root=str(output / "batches"),
                  max_parallel_trials=max_parallel_trials)
    write_json(frozen / "config/runtime.json", runtime)
    write_json(frozen / "config/batch.json", config)
    files.update({n: read_regular(frozen / n) for n in ("config/runtime.json", "config/batch.json")})
    hashes = {n: digest(data) for n, data in sorted(files.items())}
    manifest = {"format": "ci-regression-freeze-v1", "created_at": datetime.now(timezone.utc).isoformat(),
                "source_repository": str(REPO), "source_head": command(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip(),
                "source_head_is_snapshot_identity": False,
                "source_dirty_status": command(["git", "status", "--short"], cwd=REPO).decode(),
                "files": hashes, "snapshot_sha256": digest(json_bytes(hashes)), "skill_sha256": bundle_hash(skill),
                "engine_sha256": engine_digest(), "maven_cache_sha256": digest(json_bytes(cache)),
                "scope": old["scope"], "environment_source": str(previous),
                "exclusions": old["exclusions"], "external_dependencies": old["external_dependencies"]}
    write_json(output / "freeze-manifest.json", manifest)
    return {"output": str(output), "files": len(files), "skill_sha256": manifest["skill_sha256"],
            "engine_sha256": manifest["engine_sha256"], "models_called": False}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--previous", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--repair-timeout-seconds", type=int, default=900, help="new freeze repair wall limit (default: 900)")
    p.add_argument("--max-parallel-trials", type=int, choices=(1, 2, 3), default=3, help="new freeze worker limit (default: 3)")
    a = p.parse_args()
    print(json.dumps(freeze(a.previous, a.output, repair_timeout_seconds=a.repair_timeout_seconds,
                           max_parallel_trials=a.max_parallel_trials), ensure_ascii=False, indent=2))
