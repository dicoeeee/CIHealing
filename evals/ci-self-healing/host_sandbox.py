"""macOS subprocess isolation for native repair/build trials, never a cwd sandbox."""

import json
import os
from pathlib import Path
import platform
import shlex

from common import EvalError


def policy(config, *, read=(), write=(), port=None, pty=False):
    if platform.system() != "Darwin":
        raise EvalError("native repair requires macOS sandbox-exec; no unsandboxed fallback")
    roots = ["/System", "/usr", "/bin", "/sbin", "/private/var/db/dyld",
             "/Library/Developer/CommandLineTools"]
    if config.get("python"):
        python = Path(config["python"])
        roots.extend([str(python.parent.parent), str(python.resolve().parent.parent)])
    if config.get("maven"):
        roots.append(str(Path(config["maven"]).resolve().parent.parent))
    if config.get("java_home"):
        roots.append(config["java_home"])
    # Trusted host-selected tool installations for non-Java build systems.
    # No inferred tools, installation, or expansion of writable workspace roots.
    for executable in config.get("build_tools", []):
        path = Path(executable)
        if not path.is_absolute() or path.resolve().parent.parent in {Path("/"), Path.home()}:
            raise EvalError("build tool read root is not sufficiently scoped")
        roots.append(str(path.resolve().parent.parent))
    rg = config.get("rg")
    if config.get("git"):
        roots.append(str(Path(config["git"]).resolve().parent.parent))
    ancestors = sorted({str(parent) for root in [*roots, *read, *write, *([rg] if rg else [])] for parent in Path(root).parents})
    def filters(kind, paths):
        return " ".join("(" + kind + " " + json.dumps(str(p)) + ")" for p in paths)
    rows = ["(version 1)", "(allow default)", "(deny file-read*)", "(deny file-write*)", "(deny network*)",
            "(deny process-exec)", "(deny appleevent-send)", "(deny pseudo-tty)", "(allow file-read-metadata)",
            "(allow file-read* " + filters("subpath", [*roots, *read, *write]) + ")",
            "(allow file-read* " + filters("literal", [*ancestors, "/private/etc/localtime", "/private/etc/passwd"]) + ")",
            '(allow file-read* (literal "/dev/null") (literal "/dev/zero") (literal "/dev/random") (literal "/dev/urandom") (literal "/dev/dtracehelper"))',
            "(allow file-write* " + filters("subpath", write) + " (literal \"/dev/null\") (literal \"/dev/dtracehelper\"))",
            "(allow process-exec " + filters("subpath", roots) + ")"]
    if pty:
        # Same scoped mechanism as macOS application.sb: the kernel grants an
        # extension for the newly allocated slave, not every user's terminal.
        rows += ['(allow pseudo-tty)',
                 '(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))',
                 '(allow file-read* file-write* (require-all (regex #"^/dev/ttys[0-9]+$") (extension "com.apple.sandbox.pty")))']
    if rg:
        rows += ["(allow file-read* " + filters("literal", [rg]) + ")",
                 "(allow process-exec " + filters("literal", [rg]) + ")"]
    if port is not None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise EvalError("invalid local gateway port")
        rows.append('(allow network-outbound (remote tcp "localhost:' + str(port) + '"))')
    return "\n".join(rows) + "\n"


def environment(config, runtime):
    runtime = Path(runtime)
    # Do not inherit proxy, credentials, Git/Maven injection, Python startup paths,
    # or user shell startup files. Do not change HOME or global Java settings.
    env = {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME") if k in os.environ}
    env.update(PATH=os.pathsep.join([*([str(Path(config["git"]).parent)] if config.get("git") else []),
                                    str(Path(config["java_home"]) / "bin"), str(Path(config["maven"]).parent),
                                    *([str(Path(config["rg"]).parent)] if config.get("rg") else []),
                                    str(Path(config["python"]).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
               JAVA_HOME=config["java_home"], LANG="en_US.UTF-8", PYTHONDONTWRITEBYTECODE="1",
               TMPDIR=str(runtime / "tmp"), MAVEN_SKIP_RC="true",
               MAVEN_OPTS=shlex.join([f'-Duser.home={runtime / "java-home"}', f'-Djava.io.tmpdir={runtime / "tmp"}']),
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", CI_EVAL_GIT=config.get("git", ""),
               CHRYS_SESSION_TITLE_AUTO="0", CHRYS_MAX_TRANSIENT_RETRIES="1",
               CHRYS_HISTORY_DISABLE="1", CHRYS_OTEL="0", CHRYS_OTEL_SENSITIVE_DATA="0",
               CHRYS_DEBUG_LLM_RAW_HTTP_LOG="0")
    return env
