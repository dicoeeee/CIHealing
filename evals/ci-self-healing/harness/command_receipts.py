"""Small evidence-only adapter copied beside the isolated worker (stdlib only)."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex

VERSION = 'native-command-receipts-v2'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def metadata(value):
    """Copy only known typed status fields, never arbitrary metadata or error text."""
    result = {'collected': isinstance(value, dict), 'valid': isinstance(value, dict)}
    if not isinstance(value, dict):
        return result
    for key in ('shell_exit_code', 'shell_timeout_seconds', 'process_exit_code', 'process_timeout_seconds'):
        if key in value:
            if type(value[key]) is int:
                result[key] = value[key]
            else:
                result['valid'] = False
    for key in ('shell_timed_out', 'process_timed_out', 'errored', 'interrupted', 'interrupted_post_processing', 'failed'):
        if key in value:
            if type(value[key]) is bool:
                result[key] = value[key]
            else:
                result['valid'] = False
    if 'shell_background_task' in value:
        task = value['shell_background_task']
        if (isinstance(task, dict) and isinstance(task.get('id'), str)
                and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task['id'])
                and type(task.get('pid')) is int and task['pid'] > 0
                and task.get('trigger') in ('timeout', 'idle', 'user', 'shell_exited')):
            result['shell_background_task'] = {k: task[k] for k in ('id', 'pid', 'trigger')}
        else:
            result['valid'] = False
    if 'tool_error_kind' in value:
        result['tool_error_kind'] = value['tool_error_kind'] if value['tool_error_kind'] in (
            'process_stopped', 'command_blocked', 'working_dir_changed', 'working_dir_not_found', 'shell_not_found') else 'other'
    return result


def task_views(tasks):
    """A background-task end is not proof that all children stopped."""
    rows = []
    for task in tasks if isinstance(tasks, (list, tuple)) else ():
        if not isinstance(task, dict) or not isinstance(task.get('id'), str):
            continue
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task['id']):
            continue
        rows.append({'id': task['id'], 'ended': type(task.get('ended_at')) in (int, float),
                     'exit_code': task.get('exit_code') if type(task.get('exit_code')) is int else None,
                     'stopped_by': task.get('stopped_by') if task.get('stopped_by') in
                        ('agent', 'user', 'agent_finished', 'rollback', 'session_end') else None,
                     'child_absence_confirmed': None})
    return rows


def inside(path, visible, cwd):
    root = Path(visible).resolve()
    path = Path(os.path.abspath(os.path.join(str(cwd), str(path))))
    if path != root and root not in path.parents:
        return None
    if any(part.is_symlink() for part in [path, *path.parents] if part == root or root in part.parents):
        return None
    relative = str(path.relative_to(root))
    return '' if relative == '.' else relative


def maven_goals(argv):
    """Recognised project-local Maven goals, excluding modes that never build."""
    if not argv or Path(argv[0]).name not in ('mvn', 'mvnw'):
        return []
    args = argv[1:]
    if set(args) & {'-version', '--version', '-v', '-help', '--help', '-h', '-?'} or any(
            (arg.startswith('-f') and arg not in ('-fae', '-ff', '-fn')) or arg.startswith('--file') for arg in args):
        return []
    goals, values = [], iter(args)
    for word in values:
        if word in ('-s', '--settings', '-gs', '--global-settings', '-P', '--activate-profiles',
                    '-pl', '--projects', '-l', '--log-file', '-T', '--threads', '-t', '--toolchains',
                    '-gt', '--global-toolchains', '-rf', '--resume-from'):
            next(values, None)
        elif word in ('compile', 'test-compile', 'test', 'package', 'verify', 'install') or (
                not word.startswith('-') and word.endswith((':compile', ':testCompile', ':test'))):
            goals.append(word)
    return goals


def classify(command, cwd, visible):
    """A deliberately small grammar. Unsupported shell syntax is unknown, not read-only."""
    unknown = {'kind': 'unknown'}
    if not isinstance(command, str) or len(command) > 65536:
        return unknown
    lines = [line.strip() for line in command.strip().splitlines() if line.strip()]
    if len(lines) == 2 and lines[0].startswith('cd '):
        command = lines[0] + ' && ' + lines[1]
    elif len(lines) != 1:
        return unknown
    parts = command.split('&&')
    if len(parts) == 2:
        try:
            prefix = shlex.split(parts[0])
        except ValueError:
            return unknown
        if len(prefix) != 2 or prefix[0] != 'cd' or re.search(r'[$`;&|<>()!*?~\\]', prefix[1]):
            return unknown
        cwd = os.path.abspath(os.path.join(str(cwd), prefix[1]))
        command = parts[1].strip()
    elif len(parts) != 1:
        return unknown
    if re.search(r'[$`;&|<>()!*?~\\\n]', command):
        return unknown
    try:
        words = shlex.split(command)
    except ValueError:
        return unknown
    if not words or inside(cwd, visible, cwd) is None:
        return unknown
    # Only simple observation utilities. No --pre, -exec, shell snippets, etc.
    if words[0] in ('/bin/pwd', '/bin/echo', '/usr/bin/printf', '/bin/ls', '/bin/cat') and (
            len(words) == 1 or all(not w.startswith('-') or w in ('-l', '-a', '-la', '-al', '-n', '--') for w in words[1:])):
        return {'kind': 'read_only', 'cwd': str(cwd)}
    if words[0] == '/bin/cp' and len(words) == 3 and all(not w.startswith('-') for w in words[1:]):
        source, target = (inside(w, visible, cwd) for w in words[1:])
        if source is not None and target is not None and source != target:
            return {'kind': 'file_copy', 'cwd': str(cwd), 'source': source, 'target': target}
    if goals := maven_goals(words):
        return {'kind': 'build', 'cwd': str(cwd), 'project': inside(cwd, visible, cwd), 'goals': goals}
    return unknown


def inputs(snapshot, project):
    prefix = project.rstrip('/') + '/' if project else ''
    selected = {path: row for path, row in snapshot.items() if path.startswith(prefix)
            and not set(Path(path[len(prefix):]).parts) & {'.git', 'target'}
            and row.get('kind') != 'directory'}
    # Link targets and access-restricted bytes have no observed content version.
    return selected if all(row.get('kind') == 'file' for row in selected.values()) else {}


def receipt(call_id, args, raw_metadata, result, before, after, *, cwd, visible):
    observed = metadata(raw_metadata)
    workdir = args.get('workdir', args.get('working_dir', cwd)) if isinstance(args, dict) else None
    workdir = os.path.abspath(os.path.join(str(cwd), workdir)) if isinstance(workdir, str) else str(cwd)
    command = args.get('command') if isinstance(args, dict) else None
    action = classify(command, workdir, visible)
    exit_code = observed.get('shell_exit_code')
    terminal_visible = (type(exit_code) is int and isinstance(result, str)
                        and re.search(r'\[exit_code:\s*' + re.escape(str(exit_code)) + r'\]\s*\Z', result) is not None)
    row = {'schema_version': VERSION, 'call_id': call_id, 'args_sha256': digest(args),
           'action': action, 'metadata': observed, 'terminal_visible': terminal_visible,
           'scope': 'native shell tool boundary; arbitrary nested shell behavior is not classified'}
    if action['kind'] == 'file_copy':
        source, target = action['source'], action['target']
        a, b, dest = before.get(source, {}), after.get(source, {}), after.get(target, {})
        if a.get('kind') == b.get('kind') == dest.get('kind') == 'file' and a.get('sha256') == b.get('sha256'):
            row['effect'] = {'kind': 'file_copy', 'target': target, 'source_sha256': a['sha256'],
                             'target_sha256': dest['sha256'], 'target_matches_expected': a['sha256'] == dest['sha256'],
                             'agent_readback_required': True}
    elif action['kind'] == 'build':
        first, last = inputs(before, action['project']), inputs(after, action['project'])
        if first and first == last and type(exit_code) is int:
            row['effect'] = {'kind': 'build', 'project': action['project'], 'inputs_sha256': digest(first),
                             'goals': action['goals'], 'exit_code': exit_code,
                             'claim_scope': 'observed build command outcome, not proof that individual tests ran or cover the contract'}
    return row


def readback(args, result, snapshot, *, cwd, visible):
    """Bind a complete actual read_file return to captured file bytes; emit hashes only."""
    if not isinstance(args, dict) or not isinstance(args.get('path'), str) or not isinstance(result, str):
        return None
    relative = inside(args['path'], visible, cwd)
    state = snapshot.get(relative, {})
    if relative is None or state.get('kind') != 'file':
        return None
    try:
        body = (Path(visible) / relative).read_bytes()
        if hashlib.sha256(body).hexdigest() != state.get('sha256'):
            return None
        lines = body.decode('utf-8').splitlines()
    except (OSError, UnicodeError):
        return None
    returned = {}
    for line in result.splitlines():
        match = re.fullmatch(r'(\d+)\|(.*)', line)
        if match:
            number = int(match[1])
            if number in returned:
                return None
            returned[number] = match[2]
    if lines and returned == {i: line for i, line in enumerate(lines, 1)}:
        return {'schema_version': VERSION, 'path': relative, 'sha256': state['sha256'],
                'complete_return': True, 'scope': 'same-version text returned by read_file'}
    return None
