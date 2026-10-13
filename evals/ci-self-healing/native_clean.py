"""Language-neutral native clean execution in host-owned disposable workspaces.

The host supplies a plan from the actual build context. No filename-based tool
selection, shell command guessing, dependency installation or Maven fallback.
The Maven plan below belongs only to the evaluator's existing Java fixtures.
"""
from dataclasses import dataclass, field
from pathlib import Path

from common import EvalError, read_regular, run_logged, wall_budget, write_new, write_json
from host_sandbox import environment, policy
from resource_cleanup import identity

CLEAN_VERSION = "3.2.0"
CLEAN_GOAL = "org.apache.maven.plugins:maven-clean-plugin:" + CLEAN_VERSION + ":clean"


@dataclass(frozen=True)
class NativeCleanPlan:
    """Trusted host plan, not instructions parsed from build logs/model prose.

    argv/cwd/environment reuse the selected build system, configuration, module
    and sanitized toolchain environment. The sandbox still controls all writes.
    Required files are optional offline prerequisites inside the disposable root.
    """
    build_system: str
    argv: tuple
    cwd: Path
    environment: dict
    required_files: tuple = ()
    unavailable_reason: str = "offline_clean_prerequisite_missing"
    timeout_seconds: int = 30
    details: dict = field(default_factory=dict)


def maven_clean_plan(config, workspace):
    """Explicit adapter for the Java/Maven fixture, never the generic default."""
    workspace = Path(workspace)
    artifact = workspace / "m2/org/apache/maven/plugins/maven-clean-plugin" / CLEAN_VERSION
    argv = (config["maven"], "-o", "-B", "-s", str(workspace / "settings.xml"), "-gs", str(workspace / "settings.xml"),
            "-Dmaven.repo.local=" + str(workspace / "m2"), "-Dstyle.color=never",
            "-Dmaven.clean.fast=false", "-Dmaven.clean.failOnError=true", "-Dmaven.clean.skip=false",
            "-Dmaven.clean.followSymLinks=false", CLEAN_GOAL)
    return NativeCleanPlan("maven", argv, workspace / "source", environment(config, workspace),
                           required_files=tuple(artifact / f"maven-clean-plugin-{CLEAN_VERSION}.{suffix}"
                                                for suffix in ("jar", "pom")),
                           unavailable_reason="pinned_clean_plugin_not_cached", details={"goal": CLEAN_GOAL})


def validate_plan(plan, workspace):
    if (not isinstance(plan, NativeCleanPlan) or not isinstance(plan.build_system, str)
            or not plan.build_system.strip() or not isinstance(plan.argv, (list, tuple)) or not plan.argv
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in plan.argv)
            or not Path(plan.argv[0]).is_absolute()
            or type(plan.timeout_seconds) is not int or not 1 <= plan.timeout_seconds <= 30
            or not isinstance(plan.environment, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) or "\0" in k + v
                   for k, v in plan.environment.items())):
        raise EvalError("invalid host native clean plan")
    cwd = Path(plan.cwd)
    if not cwd.is_absolute() or cwd != cwd.resolve() or not cwd.is_dir() or (cwd != workspace and workspace not in cwd.parents):
        raise EvalError("clean cwd is outside the disposable workspace")
    for value in plan.required_files:
        path = Path(value)
        if not path.is_absolute() or workspace not in path.parents or ".." in path.parts:
            raise EvalError("clean prerequisite outside disposable workspace")


def clean_build(config, workspace, output, resources, *, plan=None):
    """Run a verified native clean plan; artifact reclamation stays independent.

    config supplies the same host sandbox/toolchain read roots as the build.
    Generic callers need no Maven/JDK keys; build_tools can add explicit tools.
    Unknown/no-clean build systems return a skip, then use owned reclamation.
    """
    workspace, output = Path(workspace), Path(output).absolute()
    if workspace.is_symlink() or workspace != workspace.resolve():
        raise EvalError("native clean disposable path changed")
    if not any(p == workspace and identity(p.stat()) == expected for p, expected in resources.owned):
        raise EvalError("native clean requires this invocation's disposable directory")
    if not resources.writers_stopped():
        return {"status": "skipped", "reason": "build_writers_unconfirmed"}
    if plan is None:
        return {"status": "skipped", "reason": "no_verified_native_clean_plan"}
    validate_plan(plan, workspace)
    if resources.root not in output.parents or workspace == output or workspace in output.parents or output in workspace.parents:
        raise EvalError("clean logs must be preserved outside the disposable workspace")
    context = {"build_system": plan.build_system, **plan.details}
    plan_path = resources.directory / (output.name + "-plan.json")
    write_json(plan_path, {"build_system": plan.build_system, "argv": list(plan.argv),
                          "cwd": str(plan.cwd), "timeout_seconds": plan.timeout_seconds,
                          "environment": "reuse trusted sanitized build environment; values not exported"})
    resources.evidence.append(str(plan_path.relative_to(resources.root)))
    try:
        for path in plan.required_files:
            read_regular(path)
    except (OSError, EvalError):
        return {**context, "status": "skipped", "reason": plan.unavailable_reason}
    # Immutable host policy, never a file the preceding build could edit.
    sandbox = resources.directory / (output.name + "-sandbox.sb")
    write_new(sandbox, policy(config, write=[workspace]))
    resources.evidence.append(str(sandbox.relative_to(resources.root)))
    argv = ["/usr/bin/sandbox-exec", "-f", str(sandbox), *plan.argv]
    resources.watch(output / "process.json")
    # Shutdown has a separate bounded budget; never launch another build/model.
    try:
        with wall_budget(plan.timeout_seconds):
            process = run_logged(argv, output, plan.timeout_seconds, env=plan.environment, cwd=plan.cwd)
    except (OSError, EvalError) as exc:
        return {**context, "status": "failed", "reason": "clean_execution_error", "error_type": type(exc).__name__,
                "command": argv}
    return {**context, "status": "completed" if process["status"] == "completed" and process["returncode"] == 0 else "failed",
            "command": argv, "process": process,
            "meaning": "command outcome only; resource cleanup receipt establishes final absence"}
