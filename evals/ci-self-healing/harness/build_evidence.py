"""Read straight-line build commands from public bash tool events without executing them."""
import os
import re
import shlex
from pathlib import Path


_NAME = r"[A-Za-z_][A-Za-z_0-9]*"
_VARIABLE = re.compile(r"\$(?:\{(" + _NAME + r")\}|(" + _NAME + r"))")
_LITERAL = re.compile(r"[\w./:@%+=, -]+")
_TAIL = re.compile(r"\s*\|\s*tail\s+(?:-\d+|-n\s+\d+)\s*$")
_PIPE_EXIT = re.compile(r'^echo\s+"EXIT=\$\{PIPESTATUS\[0\]\}"$')
_END = re.compile(r"\[exit_code:\s*(-?\d+)\]\s*\Z")
from .command_receipts import maven_goals


def _words(line, variables):
    """Accept literal words and references to this command's literal assignments."""
    # shlex removes quotes. Reject forms where that would change expansion,
    # splitting, escaping, or command semantics.
    if re.search(r"[\\`;&|<>()!*?~#\[\]]", line) or re.search(r"'[^']*\$[^']*'", line):
        return None
    unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "", line)
    for match in _VARIABLE.finditer(unquoted):
        value = variables.get(match.group(1) or match.group(2), "")
        if any(char.isspace() for char in value):
            return None
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    expanded = []
    for token in tokens:
        for match in _VARIABLE.finditer(token):
            name = match.group(1) or match.group(2)
            if name not in variables:
                return None
        value = _VARIABLE.sub(lambda m: variables[m.group(1) or m.group(2)], token)
        if not _LITERAL.fullmatch(value):
            return None
        expanded.append(value)
    return expanded


def _build_argv(argv):
    if not argv:
        return False
    name, args = Path(argv[0]).name, argv[1:]
    if set(args) & {"-version", "--version", "-v", "-help", "--help", "-h", "-?"}:
        return False
    if name in {"mvn", "mvnw"}:
        return bool(maven_goals(argv))
    if name == "javac":
        return any(value.endswith(".java") or value.startswith("@") for value in args)
    return name == "java" and any(not arg.startswith("-") for arg in args)


def build_receipt(command, output, *, cwd, source_dir, maven_paths):
    lines = [line.strip() for line in re.split(r"\n|&&|;", command.strip()) if line.strip()]
    capture = bool(lines and _PIPE_EXIT.fullmatch(lines[-1]))
    if capture:
        lines.pop()
    if not lines:
        return None
    variables = {}
    for statement in lines[:-1]:
        if statement.startswith("cd "):
            args = _words(statement, variables)
            if not args or len(args) != 2 or args[1].startswith("-"):
                return None
            cwd = os.path.normpath(os.path.join(cwd, args[1]))
        else:
            assignment = re.fullmatch(r"(" + _NAME + r")=(.+)", statement)
            if not assignment:
                return None
            literal = _words(assignment.group(2), {})
            if not literal or len(literal) != 1:
                return None
            variables[assignment.group(1)] = literal[0]
    invocation = lines[-1]
    piped = _TAIL.search(invocation)
    if piped:
        invocation = invocation[:piped.start()]
    elif capture:
        return None
    invocation = re.sub(r"\s+2>&1\s*$", "", invocation)
    argv = _words(invocation, variables)
    if not _build_argv(argv) or (source_dir and cwd != source_dir):
        return None
    # Bind expanded executables to the recorded Maven runtime. Only project-local
    # builds are attributed to the candidate; alternate project selectors need more evidence.
    explicit_maven = "/" in argv[0] and Path(argv[0]).name == "mvn" and any(maven_paths)
    if (variables or explicit_maven) and argv[0] not in maven_paths:
        return None
    if Path(argv[0]).name in {"mvn", "mvnw"}:
        for arg in argv[1:]:
            if arg in {"-fae", "-ff", "-fn"}:
                continue
            if arg.startswith("-f") or arg == "--file" or arg.startswith("--file="):
                return None
    terminal = _END.search(output)
    exit_code = int(terminal.group(1)) if terminal and not piped else None
    if piped and capture and terminal:
        child = re.search(r"(?:^|\n)EXIT=(\d+)\s*\Z", output[:terminal.start()])
        if child:
            exit_code = int(child.group(1))
    return {"program": argv[0], "argv": argv, "cwd": cwd, "exit_code": exit_code}
