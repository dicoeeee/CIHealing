"""Offline container entrypoint: genuine Maven compile plus independent javac oracle."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from contextlib import nullcontext


def run(argv, cwd):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    return {"command": argv, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--native-root", type=Path)
    args = parser.parse_args()
    # Copy before building: grading must never clean or repair the submitted tree.
    with (nullcontext(args.native_root) if args.native_root else tempfile.TemporaryDirectory(prefix="ci-eval-build-")) as temp:
        temp = Path(temp)
        repo = temp / "source"
        if args.native_root:
            extra = ["-s", str(temp / "settings.xml"), "-gs", str(temp / "settings.xml")]
            cache = temp / "m2"
            probe = temp / "ContractProbe.java"
        else:
            shutil.copytree("/source", repo, ignore=shutil.ignore_patterns(".git", "target"))
            shutil.copytree("/opt/maven-repository", "/tmp/m2")
            extra, cache, probe = [], Path("/tmp/m2"), Path("/oracle/ContractProbe.java")
        maven = run(["mvn", "-o", "-B", *extra, "-Dstyle.color=never", "-Dmaven.repo.local=" + str(cache), "compile"], repo)
        source_files = sorted(str(p) for p in (repo / "src/main/java").rglob("*.java"))
        classes = temp / "classes"
        classes.mkdir()
        javac = run(["javac", "-XDrawDiagnostics", "--release", "17", "-proc:none", "-d", str(classes), *source_files], repo)
        checks = {"maven": maven, "javac": javac}
        if args.baseline:
            text = maven["stdout"] + maven["stderr"]
            passed = (maven["returncode"] != 0 and javac["returncode"] != 0
                      and "Feature.java" in text and "route" in text
                      and "compiler.err.cant.apply.symbol" in javac["stderr"])
        else:
            compile_probe = run(["javac", "--release", "17", "-proc:none", "-cp", str(classes), "-d", str(classes), str(probe)], repo)
            checks["oracle_compile"] = compile_probe
            if compile_probe["returncode"] == 0:
                checks["oracle_run"] = run(["java", "-cp", str(classes), "ContractProbe"], repo)
            passed = all(check["returncode"] == 0 for check in checks.values()) and "oracle_run" in checks
        print(json.dumps({"status": "passed" if passed else "failed", "checks": checks}, ensure_ascii=False))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
