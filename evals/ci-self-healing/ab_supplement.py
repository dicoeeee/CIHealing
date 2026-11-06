"""Explicit replacement of host-invalid trials, preserving every original run.

Only a diagnosed, recorded harness disposition admits a replacement. This is
not retry-on-failure: model behaviour and verdicts never select replacements.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

from common import digest, read_json, read_regular, replace_json, write_json
from native_repair import load_config
from ab_run import DEFAULT_ROOT, one
from ab_runtime import prepare_trial

ENGINE = Path(__file__).resolve().parent


def prepare(root, revision):
    root = Path(root).resolve()
    manifest_path = root / f"supplemental-r{revision}-manifest.json"
    if manifest_path.exists():
        raise ValueError("supplement already frozen")
    config = load_config(read_json(root / "runtime.json"))
    entries = []
    for version in ("A", "B"):
        rows = read_json(root / version / "manifest.json")["trials"]
        for row in rows:
            original = Path(row["trial"])
            disposition_path = original / "harness-disposition.json"
            if not disposition_path.exists():
                continue
            disposition = read_json(disposition_path)
            target_revision = disposition.get("replacement_harness_revision", 2)
            if target_revision != revision:
                continue
            trial = root / f"supplemental-r{revision}" / version / "trials" / row["id"]
            source = root / ("baseline" if version == "A" else "candidate") / "skills/ci-self-healing"
            template = root / version / "templates/handoff/trials" / row["scenario"] if row["group"] == "handoff" else None
            record = prepare_trial(trial, row["group"], row["scenario"], source, config, template=template)
            if row["group"] == "enterprise" and revision >= 3:
                layout = read_json(trial / "prepared-layout.json")
                layout.update(enterprise_resource_registration=True, enterprise_resource_scenario=row["scenario"])
                replace_json(trial / "prepared-layout.json", layout)
                (trial / "adapter/ab_skill_host.py").write_bytes(read_regular(ENGINE / "ab_skill_host.py"))
            entry = {"version": version, "id": row["id"], "group": row["group"], "scenario": row["scenario"],
                     "repeat": row["repeat"], "original_trial": str(original), "replacement_trial": str(trial),
                     "reason": disposition["reason"], "harness_revision": revision,
                     "original_disposition_sha256": digest(read_regular(disposition_path)), "trial": str(trial), **record}
            write_json(trial / "replacement-binding.json", entry)
            entries.append(entry)
    if not entries:
        raise ValueError("no recorded host-invalid trials for revision")
    manifest = {"revision": revision, "entries": entries,
                "selection": "all recorded affected starts, independent of model verdict; originals retained"}
    write_json(manifest_path, manifest)
    combined = []
    for p in sorted(root.glob("supplemental-r*-manifest.json")):
        combined.extend(read_json(p)["entries"])
    replace_json(root / "replacements.json", {"entries": combined})
    print(json.dumps({"prepared_replacements": len(entries), "revision": revision}), flush=True)
    return manifest


def run(root, revision, jobs):
    root = Path(root).resolve()
    if not 1 <= jobs <= 6:
        raise ValueError("jobs outside shared cap")
    manifest = read_json(root / f"supplemental-r{revision}-manifest.json")
    config = load_config(read_json(root / "runtime.json"))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(one, root, row["version"] + f"-supplement-r{revision}", row, config)
                   for row in manifest["entries"]]
        for future in as_completed(futures):
            print(json.dumps(future.result(), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run"])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--execute-live", action="store_true")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.root, args.revision)
    else:
        if not args.execute_live:
            parser.error("--execute-live required")
        run(args.root, args.revision, args.jobs)
