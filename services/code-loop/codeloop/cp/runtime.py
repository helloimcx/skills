"""Trusted local configuration and evidence collection. No agent-supplied commands."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            text=True, encoding='utf-8', timeout=30)
    if result.returncode:
        raise ValueError('GIT_FAILED: ' + result.stderr.strip())
    return result.stdout.strip()


def version(value, policy):
    raw = json.dumps([value, policy], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


# The repository exam — which commands decide whether a change passes. Read
# only from the project's base branch at a pinned commit id (never a PR
# branch), so no pull request can alter the exam it is judged by.
EXAM_PATH = '.code-loop/verify.json'
EXAM_MAX_BYTES = 64 * 1024
_EXAM_KEYS = ('verification', 'verification_fast', 'required_checks')


def _argv_template(template):
    return (isinstance(template, list) and bool(template)
            and all(isinstance(x, str) for x in template))


def resolve_verification(cfg, kind):
    """Kind 分层（2026-09-21，全量测试 <1min 目标的 P3）：reviewer 轮保持全量
    考题（人工合并前的正确性地板）；其余 kind 走 ``verification_fast`` 加速层，
    考题未声明时回落全量——分层只允许让轮次更快，不允许让门禁消失。"""
    full = cfg.get('verification') or []
    if kind == 'reviewer':
        return full
    return cfg.get('verification_fast') or full


def _argv_list(data, key, error):
    """考题里 argv 模板列表键的统一校验（verification / verification_fast）。"""
    value = data.get(key, [])
    if not isinstance(value, list) or not all(_argv_template(t) for t in value):
        raise ValueError(error)
    return value


def parse_exam(body):
    """Strict parse of the repository exam file. Fail closed.

    Every deviation raises instead of degrading the exam: a malformed file
    must stall the pipeline loudly, never silently fall back to the machine
    configuration the repository exam replaced. Machine facts (interpreter,
    timeouts, paths) stay out of the repository, so unknown keys are refused.
    """
    if not isinstance(body, str) or len(body.encode('utf-8')) > EXAM_MAX_BYTES:
        raise ValueError('EXAM_INVALID: exam must be text under %d bytes' % EXAM_MAX_BYTES)
    try:
        data = json.loads(body)
    except ValueError:
        raise ValueError('EXAM_INVALID: exam is not JSON') from None
    if not isinstance(data, dict):
        raise ValueError('EXAM_INVALID: exam must be a JSON object')
    unknown = sorted(set(data) - set(_EXAM_KEYS))
    if unknown:
        raise ValueError('EXAM_INVALID: unknown keys ' + ','.join(unknown))
    verification = _argv_list(data, 'verification', 'VERIFICATION_MUST_BE_ARGV')
    # 加速层（resolve_verification 的另一半）：与全量考题同构、同校验。声明才
    # 出键——未声明的项目策略 hash 逐字节不变（零扰动，对齐 interpreter 惯例）。
    result = {'verification': verification}
    if 'verification_fast' in data:
        result['verification_fast'] = _argv_list(
            data, 'verification_fast', 'VERIFICATION_FAST_MUST_BE_ARGV')
    required_checks = data.get('required_checks', [])
    if not isinstance(required_checks, list) or not all(
            isinstance(x, str) and x.strip() for x in required_checks):
        raise ValueError('EXAM_INVALID: required_checks must be a list of context names')
    # Normalized: a stray space would never match a platform context and the
    # round would fail closed forever on a typo the platform cannot explain.
    result['required_checks'] = [x.strip() for x in required_checks]
    return result


def default_worker(loop, hostname=None):
    """Local claim label when --worker is omitted: ``<hostname>/<loop>``.

    Sessions used to pass --worker by hand, so the same machine appeared in
    executions, lock files and the dashboard under drifting labels
    (reviewer-dev / handler / handler-main-agent), one of them even claiming a
    reviewer item under a handler name. Remote carriers send the bare loop
    instead: the CP server prefixes the enrolled machine name itself
    (cp/server.py), so the CLI must not compose a second host prefix there.
    """
    host = (hostname if hostname is not None else socket.gethostname()).strip()
    return '%s/%s' % (host or 'unknown-host', loop)


def _resolve_state_dirs(cfg, config_dir, repo_root=None):
    # Worktrees default next to the project itself (one managed root per
    # repo); configs without a local repo keep the config-directory default,
    # which never takes part in path checks (bind facts are worker-attested).
    worktree_default = Path(repo_root) / '.git-worktrees' if repo_root else config_dir / 'worktrees'
    for key, default in [('state_dir', config_dir / 'state'), ('worktree_root', worktree_default)]:
        value = Path(cfg.get(key, default)).expanduser()
        cfg[key] = str((config_dir / value).resolve() if not value.is_absolute() else value.resolve())


def _validate_numeric(cfg):
    for key in ('lease_seconds', 'max_parallel', 'snapshot_ttl', 'discovery_window_seconds',
                'verify_timeout', 'baseline_window_seconds', 'baseline_max_candidates',
                'gate_repair_max_attempts', 'blocked_probe_seconds',
                'blocked_lookback_seconds'):
        if not isinstance(cfg[key], (int, float)) or isinstance(cfg[key], bool) or cfg[key] <= 0:
            raise ValueError('INVALID_CONFIG: ' + key)
    if cfg['lease_seconds'] < 3600:
        raise ValueError('INVALID_CONFIG: lease_seconds must be at least 3600')
    # Orphan-collapse idle threshold: 0 opts out, anything active must stay
    # far above the longest legitimate silent stretch of a healthy round.
    value = cfg['orphan_idle_seconds']
    if not isinstance(value, int) or isinstance(value, bool) or (value != 0 and value < 600):
        raise ValueError('INVALID_CONFIG: orphan_idle_seconds must be 0 or at least 600')


def _validate_verify_timeout(cfg):
    if not isinstance(cfg['verify_timeout'], (int, float)) or isinstance(cfg['verify_timeout'], bool) \
            or cfg['verify_timeout'] <= 0:
        raise ValueError('INVALID_CONFIG: verify_timeout')


def _pin_interpreter(cfg):
    if cfg.get('interpreter') is not None:
        if not isinstance(cfg['interpreter'], str):
            raise ValueError('INVALID_CONFIG: interpreter')
        interpreter = Path(cfg['interpreter']).expanduser()
        if not interpreter.is_absolute() or not interpreter.is_file():
            raise ValueError('INTERPRETER_MISSING: ' + str(interpreter))
        # Keep symlinks intact (POSIX venv interpreters locate their site
        # packages via argv[0]); only normalize the path, never resolve links.
        cfg['interpreter'] = os.path.abspath(str(interpreter))


def _load_platform(cfg, config_dir):
    platform = cfg.get('platform', {})
    if not platform.get('url') or not platform.get('repo'):
        raise ValueError('PLATFORM_REQUIRED')
    token_env = platform.get('token_env', 'GITHUB_TOKEN')
    platform['token_env'] = token_env
    # Machine-scoped credential file: read per invocation, no host restart needed.
    platform['token_file'] = str((Path.home() / '.code-loop' / 'credentials' / token_env).resolve())
    if platform.get('ca_file'):
        ca = Path(platform['ca_file']).expanduser()
        ca = (config_dir / ca).resolve() if not ca.is_absolute() else ca.resolve()
        if not ca.is_file():
            raise ValueError('CA_FILE_MISSING: ' + str(ca))
        platform['ca_file'] = str(ca)
    # The adapter no longer consults this field (PR statuses are always
    # observed); the dashboard still reads it from the project config to label
    # the checks it displays.
    cfg['platform'] = dict(platform, base=cfg['base'], required_checks=cfg['required_checks'])


def _load_coordinator_config(cfg, config_dir):
    """Shared tail of local and server-side project configs: full policy authority."""
    _resolve_state_dirs(cfg, config_dir, cfg.get('repo_root'))
    # The project key is the config directory name (one config per project
    # under ~/.code-loop/projects/<key>/): the observer store keys facts by
    # it, and the dashboard derives the same key the same way — deriving it
    # unconditionally keeps the two consumers on one partition.
    cfg['project_key'] = config_dir.name
    cfg.setdefault('base', 'main')
    cfg.setdefault('branch_prefixes', ['feature/mocx/', 'fix/mocx/'])
    cfg.setdefault('verification', [])
    cfg.setdefault('interpreter', None)
    cfg.setdefault('required_checks', [])
    cfg.setdefault('lease_seconds', 14400)
    cfg.setdefault('max_parallel', 3)
    cfg.setdefault('orphan_idle_seconds', 7200)
    cfg.setdefault('snapshot_ttl', 300)
    cfg.setdefault('discovery_window_seconds', 10800)
    cfg.setdefault('verify_timeout', 120)
    cfg.setdefault('baseline_window_seconds', 7 * 86400)
    cfg.setdefault('baseline_max_candidates', 5)
    cfg.setdefault('verify_timeout', 120)
    cfg.setdefault('baseline_window_seconds', 7 * 86400)
    cfg.setdefault('baseline_max_candidates', 5)
    cfg.setdefault('verify_rerun', True)
    cfg.setdefault('verify_attribution', True)
    cfg.setdefault('gate_repair_max_attempts', 3)
    cfg.setdefault('blocked_probe_seconds', 6 * 3600)
    cfg.setdefault('blocked_lookback_seconds', 30 * 86400)
    cfg.setdefault('gate_command_allow', ['make'])
    if not isinstance(cfg['gate_command_allow'], list) or \
            not all(isinstance(x, str) and x.strip() for x in cfg['gate_command_allow']):
        raise ValueError('INVALID_CONFIG: gate_command_allow')
    for flag in ('verify_rerun', 'verify_attribution'):
        if not isinstance(cfg[flag], bool):
            raise ValueError('INVALID_CONFIG: ' + flag)
    _validate_numeric(cfg)
    _pin_interpreter(cfg)
    _load_platform(cfg, config_dir)
    cfg['mode'] = 'local'
    return cfg


def _loopback(host):
    host = (host or '').lower()
    return host in ('localhost', '::1') or host.startswith('127.')


def _load_worker_server(server, base_dir):
    """Validate and resolve the machine-global server pointer. No routing key
    here: the project comes from the working directory or an explicit
    ``server.project`` pin (checked by the caller)."""
    if not isinstance(server, dict):
        raise ValueError('INVALID_CONFIG: server')
    url = server.get('url')
    if not isinstance(url, str) or not url.strip():
        raise ValueError('SERVER_URL_REQUIRED')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('INVALID_CONFIG: server.url')
    if parsed.scheme != 'https' and not _loopback(parsed.hostname):
        raise ValueError('SERVER_URL_REQUIRES_TLS')
    resolved = {'url': url}
    token_env = server.get('token_env', 'CODE_LOOP_CP_TOKEN')
    if not isinstance(token_env, str) or not token_env:
        raise ValueError('INVALID_CONFIG: server.token_env')
    resolved['token_env'] = token_env
    if server.get('ca_file'):
        ca = Path(server['ca_file']).expanduser()
        ca = (base_dir / ca).resolve() if not ca.is_absolute() else ca.resolve()
        if not ca.is_file():
            raise ValueError('CA_FILE_MISSING: ' + str(ca))
        resolved['ca_file'] = str(ca)
    timeout = server.get('timeout', 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError('INVALID_CONFIG: server.timeout')
    resolved['timeout'] = timeout
    project = server.get('project')
    if project is not None:
        if not isinstance(project, str) or not project:
            raise ValueError('INVALID_CONFIG: server.project')
        resolved['project'] = project
    return resolved


def data_home():
    """Machine-local code-loop data home (~/.code-loop), redirectable for tests."""
    return Path(os.environ.get('CODE_LOOP_HOME') or (Path.home() / '.code-loop')).expanduser()


def worker_global_path():
    return data_home() / 'control-plane.json'


def _coordination_root(cwd):
    root = Path(git(cwd or Path.cwd(), 'rev-parse', '--show-toplevel')).resolve()
    common = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    if common.name != '.git':
        raise ValueError('NONSTANDARD_GIT_LAYOUT: set up a normal local clone')
    # All linked worktrees of one repo share this coordination root.
    return common.parent


def _project_key_from_origin(cwd):
    url = git(cwd, 'remote', 'get-url', 'origin')
    if re.match(r'^[A-Za-z]:[\\/]', url) or url.startswith('/'):
        raise ValueError('PROJECT_KEY_UNRESOLVED: origin "%s" is a local path; '
                         'set "server.project" in the global worker config' % url)
    m = re.search(r'[:/]([^/:\\]+/[^/\\]+?)(?:\.git)?/?$', url)
    if not m:
        raise ValueError('PROJECT_KEY_UNRESOLVED: origin "%s" is not <org>/<repo>' % url)
    return m.group(1).replace('/', '__')


def _read_global_worker_file(path=None):
    path = Path(path or worker_global_path()).expanduser().resolve()
    if not path.is_file():
        raise ValueError('GLOBAL_WORKER_CONFIG_MISSING: ' + str(path)
                         + ' (one global config per worker machine names the CP server)')
    cfg = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(cfg, dict):
        raise ValueError('INVALID_CONFIG: global worker config must be a JSON object')
    return path, cfg


def load_worker_server(path=None):
    """Server pointer only — no project-key derivation, safe from any cwd.

    The resident agent reads this once at startup; a scheduled task's cwd is
    not a git repo, and no project key is meaningful until a command names a
    project (per-command loads pin their own cwd via the registration's
    repo_cwd and go through load_global_worker_config).
    """
    path, cfg = _read_global_worker_file(path)
    return _load_worker_server(cfg.get('server'), path.parent)


def load_global_worker_config(path=None, cwd=None):
    """Resolve the machine-global worker config against the working directory.

    One file per worker machine (~/.code-loop/control-plane.json) names the CP
    server and this machine's execution facts; the project key comes from the
    working directory's git origin (or an explicit ``server.project`` pin).
    Policy never lives here — it arrives with each claim and is cached under
    the project's state directory.
    """
    path, cfg = _read_global_worker_file(path)
    server = _load_worker_server(cfg.get('server'), path.parent)
    root = _coordination_root(cwd)
    key = server.get('project') or _project_key_from_origin(root)
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', key) \
            or key in ('.', '..'):
        raise ValueError('INVALID_CONFIG: project key')
    config_dir = path.parent / 'projects' / key
    merged = _merge_machine_config(cfg, key)
    _inherit_verify_timeout(merged, config_dir)
    merged['repo_root'] = str(root)
    _resolve_state_dirs(merged, config_dir, root)
    if 'verify_timeout' in merged:
        _validate_verify_timeout(merged)
    _validate_machine_flags(merged)
    _pin_interpreter(merged)
    merged['server'] = dict(server, project=key)
    merged['project'] = key
    merged['mode'] = 'worker'
    return merged


_MACHINE_FIELDS = ('state_dir', 'worktree_root', 'interpreter', 'verify_timeout',
                   'verify_rerun', 'verify_attribution', 'gate_command_allow')


def _merge_machine_config(cfg, key):
    """Machine-wide fields plus this project's overrides (unknown rejected)."""
    merged = {k: cfg[k] for k in _MACHINE_FIELDS if k in cfg}
    overrides = cfg.get('projects', {}).get(key, {})
    if not isinstance(overrides, dict):
        raise ValueError('INVALID_CONFIG: projects section')
    unknown = set(overrides) - set(_MACHINE_FIELDS)
    if unknown:
        raise ValueError('INVALID_CONFIG: projects.%s fields %s' % (key, ','.join(sorted(unknown))))
    merged.update(overrides)
    return merged


def _validate_machine_flags(cfg):
    """Boolean loop flags and the gate-command allowlist."""
    for flag in ('verify_rerun', 'verify_attribution'):
        if flag in cfg and not isinstance(cfg[flag], bool):
            raise ValueError('INVALID_CONFIG: ' + flag)
        cfg.setdefault(flag, True)
    cfg.setdefault('gate_command_allow', ['make'])
    if not isinstance(cfg['gate_command_allow'], list) or \
            not all(isinstance(x, str) and x.strip() for x in cfg['gate_command_allow']):
        raise ValueError('INVALID_CONFIG: gate_command_allow')


def _inherit_verify_timeout(merged, config_dir):
    """issue #200：verify_timeout 双真相源收敛——项目 config 为权威，全局
    projects.<key> 仅作 override；全局未显式给出时缺省继承项目值，避免
    同一条 verify 命令因解析出的 personality 不同拿到不同预算。"""
    if 'verify_timeout' in merged:
        return
    project_cfg = config_dir / 'control-plane.json'
    if not project_cfg.is_file():
        return
    try:
        project_timeout = json.loads(
            project_cfg.read_text(encoding='utf-8')).get('verify_timeout')
    except ValueError:
        return
    if project_timeout is not None:
        merged['verify_timeout'] = project_timeout


def load_config(path, cwd=None):
    """Resolve a machine-local project config against the working directory.

    The config lives outside any repository (~/.code-loop/projects/<key>/) and
    never names the repo: the repo comes from the caller's working directory,
    while state and the certificate default next to the config file and
    worktrees next to the repo itself.
    This is the coordinator/local personality — worker routing lives only in
    the machine-global file (load_global_worker_config).
    """
    path = Path(path).expanduser().resolve()
    cfg = json.loads(path.read_text(encoding='utf-8'))
    if 'server' in cfg:
        raise ValueError('SERVER_FIELD_REMOVED: worker machines point at the CP server '
                         'through the machine-global config; delete "server" from this '
                         'project file')
    cfg['repo_root'] = str(_coordination_root(cwd))
    return _load_coordinator_config(cfg, path.parent)


def load_server_config(path):
    """Coordinator-side project config: full policy authority, no repo required.

    The daemon may host a project whose clone lives on another machine, so no
    working-directory git derivation happens; worktree facts arrive per
    request as worker-attested context instead.
    """
    path = Path(path).expanduser().resolve()
    cfg = json.loads(path.read_text(encoding='utf-8'))
    if 'server' in cfg:
        raise ValueError('SERVER_FIELD_ON_COORDINATOR: worker configs belong on workers')
    if 'remote' in cfg and not isinstance(cfg['remote'], bool):
        raise ValueError('INVALID_CONFIG: remote')
    return _load_coordinator_config(cfg, path.parent)


# Worktree names the loops themselves create (issue #250): handler issue
# tasks use issue-<n>, reviewer review-pr-<n>, conflict repair repair-pr-<n>
# or repair-<n>, gate repairs gate-<sig>. Anything else under worktree_root
# is a human worktree and must never be bound by a round.
_OWNED_WORKTREE = re.compile(r'^(?:issue|review-pr|repair-pr|repair|gate)-\d')


def _check_worktree_ownership(wt):
    """Ownership guard (issue #250): only worktrees the loops' own naming
    scheme creates may be bound. A human worktree named after its branch
    (e.g. dash-execution-history) collides with a PR of the same slug and
    used to be claimed and force-deleted by rounds; the name check turns the
    collision into a loud refusal instead of a silent takeover."""
    if not _OWNED_WORKTREE.match(wt.name):
        raise ValueError('WORKTREE_OWNERSHIP_REJECTED')


def validate_worktree(cfg, worktree, branch=None):
    root = Path(cfg['repo_root']).resolve()
    wt = Path(worktree).resolve()
    allowed = Path(cfg['worktree_root']).resolve()
    if wt == root or allowed not in wt.parents:
        raise ValueError('WORKTREE_ISOLATION_FAILED')
    _check_worktree_ownership(wt)
    common = git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if git(wt, 'rev-parse', '--path-format=absolute', '--git-common-dir') != common:
        raise ValueError('WRONG_REPOSITORY')
    registered = [Path(line[9:]).resolve() for line in git(root, 'worktree', 'list', '--porcelain').splitlines()
                  if line.startswith('worktree ')]
    # Git prints forward slashes even when the OS separator differs (Windows);
    # compare through Path so separator style never decides registration.
    if wt not in registered or Path(git(wt, 'rev-parse', '--show-toplevel')).resolve() != wt:
        raise ValueError('UNREGISTERED_WORKTREE')
    actual = git(wt, 'branch', '--show-current')
    if branch is not None and actual != branch:
        raise ValueError('BRANCH_MISMATCH')
    if actual and (actual == cfg['base'] or not any(actual.startswith(p) for p in cfg['branch_prefixes'])):
        raise ValueError('BRANCH_NOT_ALLOWED')
    if git(wt, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('DIRTY_WORKTREE')
    return {'worktree': str(wt), 'branch': actual, 'head': git(wt, 'rev-parse', 'HEAD')}


_ENV_SIGNATURES = (
    (re.compile(r"No module named '([^']+)'"), lambda m: 'no-module:' + m.group(1)),
    (re.compile(r"cannot import name '([^']+)' from '([^']+)'"),
     lambda m: 'import-name:%s.%s' % (m.group(2), m.group(1))),
)
# A raised import error opens its traceback line with the class name; mentions
# inside assertion messages ("DID NOT RAISE ImportError") are code failures.
_ENV_ERROR_LINE = re.compile(r'^(?:ModuleNotFoundError|ImportError):', re.MULTILINE)

# pytest short-summary lines ("FAILED node - reason"), present even under -q;
# they ride the evidence so baseline derivation never needs the raw log back.
_SUMMARY_LINE = re.compile(r'^(?:FAILED|ERROR)\s+(\S+)')
_FAILED_NODES_CAP = 50


def _extract_failed_nodes(log_text):
    nodes, sample = [], None
    for line in log_text.splitlines():
        found = _SUMMARY_LINE.match(line)
        if not found:
            continue
        if sample is None:
            sample = line.strip()[:200]
        node = found.group(1)[:200]
        if node not in nodes:
            nodes.append(node)
        if len(nodes) >= _FAILED_NODES_CAP:
            break
    return nodes, sample


def classify_check(exit_code, log_text):
    """Deterministic runner-side triage of a failed verification command.

    This feeds env-failure dedup counting and flags that verification could
    not be constructed at all; the reviewing agent still reads the full log
    and owns the verdict.
    """
    if exit_code == 0:
        return 'pass', None
    if exit_code == 124:
        return 'timeout', None
    for pattern, signature in _ENV_SIGNATURES:
        found = pattern.search(log_text)
        if found:
            return 'env', signature(found)
    if exit_code == 127 or _ENV_ERROR_LINE.search(log_text):
        return 'env', None
    return 'code', None


def _verify_env():
    """The verify subprocess must not inherit a polluted interpreter
    environment: a stray PYTHONHOME (e.g. pointing at a different CPython)
    makes the venv interpreter load a foreign stdlib and crash with "SRE
    module mismatch" (issue #173/#267). Copy the runner env but drop the
    variables that can redirect stdlib/site resolution — including uv's
    shim-injected ``UV_INTERNAL__PYTHONHOME``, which re-points python at
    the uv-managed CPython root the same way PYTHONHOME does."""
    return {k: v for k, v in os.environ.items()
            if k not in ('PYTHONHOME', 'PYTHONPATH', 'UV_INTERNAL__PYTHONHOME')}


def _run_command(argv, cwd, log, timeout, env, header_lines=()):
    """Run one command with stdout+stderr captured into ``log``.
    Returns the raw run record; classification happens in _classify_result."""
    started = time.time()
    with log.open('w', encoding='utf-8') as stream:
        for line in header_lines:
            stream.write(line)
        try:
            result = subprocess.run(argv, cwd=str(cwd), stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=timeout, shell=False, env=env)
            code = result.returncode
        except subprocess.TimeoutExpired:
            stream.write('\nVERIFICATION_TIMEOUT\n')
            code = 124
        except OSError as exc:
            stream.write(str(exc))
            code = 127
    raw = log.read_bytes()
    return {'exit_code': code, 'log': str(log), 'text': raw.decode('utf-8', errors='replace'),
            'sha256': hashlib.sha256(raw).hexdigest(), 'started_at': started}


def _classify_result(result, argv, cwd, interpreter):
    """Turn a raw run record into the evidence check dict (deterministic
    runner-side triage; the reviewing agent still owns the verdict)."""
    failure_class, signature = classify_check(result['exit_code'], result['text'])
    if failure_class == 'env' and signature is None:
        # Hash the log body only: the interpreter header lines depend on
        # config, and a config edit must not change the same failure's
        # dedup signature.
        body = '\n'.join(line for line in result['text'].splitlines()
                         if not line.startswith('# verify interpreter:') and not line.startswith('# WARN:'))
        signature = 'env:' + hashlib.sha1(body.encode('utf-8')).hexdigest()[:12]
    check = {'argv': argv, 'cwd': str(cwd), 'exit_code': result['exit_code'], 'log': result['log'],
             'sha256': result['sha256'], 'started_at': result['started_at'],
             'interpreter': interpreter, 'failure_class': failure_class}
    if failure_class in ('code', 'timeout'):
        failed_nodes, failed_line = _extract_failed_nodes(result['text'])
        if failed_nodes:
            check['failed_nodes'] = failed_nodes
            check['failed_line'] = failed_line
    return check, signature


def _attrib_decision(merge_base_passed, base_tip_passed):
    """Pure attribution tree for a red that survived the rerun, judged
    against base: pre-existing at the fork point and fixed at the tip means
    the branch is stale (sync-base cures it); broken at both points is a
    base defect (never the PR's); green at the fork point is the PR's own."""
    if not merge_base_passed:
        return 'stale-branch' if base_tip_passed else 'base-defect'
    return 'pr-introduced'


def _run_nodes_at(cfg, wt, argv, nodes, ref, evidence_dir):
    """Run the failed nodes once at ``ref`` in a throwaway detached worktree.
    Returns None on ANY problem — attribution must never turn a verification
    failure into a crash."""
    target = Path(cfg['worktree_root']) / '_attrib' / ('%s-%d' % (ref[:12], time.time_ns()))
    try:
        git(wt, 'worktree', 'add', '--detach', str(target), ref)
    except ValueError:
        return None
    try:
        # The evidence dir is ours; _run_command only opens the log inside it.
        Path(evidence_dir).mkdir(parents=True, exist_ok=True)
        run = _run_command(argv + list(nodes), target, evidence_dir / 'run.log',
                           cfg.get('verify_timeout', 120), _verify_env())
        return {'ref': ref, 'passed': run['exit_code'] == 0, 'exit_code': run['exit_code'],
                'sha256': run['sha256']}
    except OSError:
        return None
    finally:
        try:
            git(wt, 'worktree', 'remove', '--force', str(target))
        except ValueError:
            # Windows file locks can keep the checkout alive; prune the
            # registration and drop the tree so repeats cannot accumulate.
            try:
                shutil.rmtree(str(target), ignore_errors=True)
                git(wt, 'worktree', 'prune')
            except (ValueError, OSError):
                pass
        try:
            target.parent.rmdir()  # last one out drops the empty _attrib dir
        except OSError:
            pass


def _relativize_argv(argv, wt):
    """Rewrite the task worktree's absolute path out of an argv template.

    {worktree} templates carry the task worktree's absolute path; at another
    ref that path would re-run THIS tree and misattribute. The checkout
    layout is identical across refs, so the relative location is the right
    operand — and an operand absent from the task tree is absent at the refs
    too (caller aborts attribution)."""
    wt_prefix = str(wt)
    rewritten = []
    for token in argv:
        for prefix in (wt_prefix + os.sep, wt_prefix + '/'):
            if token.startswith(prefix):
                token = token[len(prefix):]
                break
        else:
            if token == wt_prefix:
                token = '.'
        rewritten.append(token)
    return rewritten


def _nodes_exist(argv, wt):
    for arg in argv[1:]:
        if arg and not arg.startswith('-') and not os.path.isabs(arg) \
                and arg.endswith(('.py', '.sh', '.ps1')):
            if not (Path(wt) / arg).is_file():
                return False
    return True


def _attribute_to_base(cfg, wt, checks, evidence_dir):
    """Is a survived-red about this PR, or about the gate? Bounded and
    deterministic: the failed nodes rerun at the merge-base and at the base
    tip. base-defect feeds the discover baseline lane; stale-branch lets
    derive mint a sync-base repair; pr-introduced is an honest Validation
    failed. No extractable node list (non-pytest commands) keeps the legacy
    behavior — attribution is best-effort by design."""
    if not cfg.get('verify_attribution', True):
        return None
    try:
        probe = next(({'argv': c['argv'], 'nodes': c['failed_nodes']} for c in checks
                      if c.get('failure_class') in ('code', 'timeout') and c.get('failed_nodes')), None)
        if probe is None:
            return None
        argv = _relativize_argv(probe['argv'], wt)
        if not _nodes_exist(argv, wt):
            return None
        base_ref = 'origin/' + cfg['base']
        # A stale local ref would misattribute; fetch FIRST, then compute the
        # fork point so both replay targets come from the same ref state.
        try:
            git(wt, 'fetch', 'origin', cfg['base'])
        except ValueError:
            pass
        merge_base = git(wt, 'merge-base', base_ref, 'HEAD')
        evidence_dir = Path(evidence_dir)
        runs = {'merge_base': _run_nodes_at(cfg, wt, argv, probe['nodes'], merge_base,
                                            evidence_dir / 'attrib-merge-base'),
                'base_tip': _run_nodes_at(cfg, wt, argv, probe['nodes'], base_ref,
                                          evidence_dir / 'attrib-base-tip')}
        if any(run is None for run in runs.values()):
            return None
        base_tip_sha = git(wt, 'rev-parse', base_ref)
    except (ValueError, OSError):
        # Best-effort by design: any surprise keeps the legacy verdict path.
        return None
    return {'verdict': _attrib_decision(runs['merge_base']['passed'], runs['base_tip']['passed']),
            'merge_base': merge_base, 'base_ref': base_ref, 'base_tip_sha': base_tip_sha,
            'nodes': probe['nodes'], 'runs': runs}


def _allowed_executable(exe, allowed):
    """Allowlist match on the RESOLVED path: a raw-prefix compare would let
    ``<dir>/../../evil`` ride a directory entry past the gate."""
    resolved = os.path.realpath(exe)
    for entry in allowed:
        resolved_entry = os.path.realpath(entry)
        if resolved == resolved_entry:
            return True
        if os.path.isdir(resolved_entry) and                 resolved.startswith(resolved_entry.rstrip(os.sep) + os.sep):
            return True
    return False


def _check_gate_commands(cfg, commands):
    """Validation-only half of run_gate_commands: argv shape plus the
    executable allowlist (fail closed — a missing allowlist admits nothing)."""
    if not commands or not all(isinstance(c, list) and c
                               and all(isinstance(x, str) and x.strip() for x in c)
                               for c in commands):
        raise ValueError('GATE_COMMANDS_MUST_BE_ARGV')
    allowed = cfg.get('gate_command_allow')
    if not isinstance(allowed, list) or not allowed or \
            not all(isinstance(x, str) and x.strip() for x in allowed):
        raise ValueError('GATE_COMMAND_ALLOWLIST_REQUIRED')
    for template in commands:
        if not _allowed_executable(template[0], allowed):
            raise ValueError('GATE_COMMAND_NOT_ALLOWED: ' + template[0])


def run_gate_commands(cfg, commands, cwd, evidence_dir):
    """Deterministic executor for GATE_REPAIR whitelisted actions.

    Commands are argv templates from the reviewer's structured gate report
    (``{repo_root}`` placeholder allowed); they run in the project checkout,
    never through a shell. The executable must be allowlisted by machine
    config (``gate_command_allow``) — a report-suggested command outside the
    allowlist fails closed as GATE_COMMAND_NOT_ALLOWED and counts as a failed
    repair attempt. Evidence shape mirrors run_checks so the report gate can
    demand a green run before accepting a repair."""
    _check_gate_commands(cfg, commands)
    root = Path(cwd)
    if not root.is_dir():
        raise ValueError('GATE_CWD_MISSING')
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    timeout = cfg.get('verify_timeout', 120)
    checks = []
    for i, template in enumerate(commands):
        argv = [x.replace('{repo_root}', str(root)) for x in template]
        run = _run_command(argv, root, evidence_dir / ('gate-%d.log' % i), timeout, _verify_env())
        check, _signature = _classify_result(run, argv, root, argv[0])
        checks.append(check)
    return {'passed': all(c['exit_code'] == 0 for c in checks), 'checks': checks}


def run_checks(cfg, context, evidence_dir):
    commands = cfg['verification']
    if not commands:
        raise ValueError('NO_VERIFICATION_CONFIGURED')
    wt = Path(context['worktree'])
    validate_worktree(cfg, wt, context['branch'])
    if git(wt, 'rev-parse', 'HEAD') != context['head']:
        raise ValueError('HEAD_CHANGED')
    resolved_python = cfg.get('interpreter') or sys.executable
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    verify_env = _verify_env()
    checks = []
    env_signature = None
    rerun_on = cfg.get('verify_rerun', True)
    for i, template in enumerate(commands):
        if not isinstance(template, list) or not template or not all(isinstance(x, str) for x in template):
            raise ValueError('VERIFICATION_MUST_BE_ARGV')
        argv = [x.replace('{worktree}', str(wt)).replace('{python}', resolved_python) for x in template]
        # Interpreter paths may be external; script/file operands must stay inside the task.
        for arg in argv[1:]:
            if arg.endswith(('.py', '.sh', '.ps1')) or (os.path.isabs(arg) and not arg.startswith('-')):
                candidate = (wt / arg).resolve()
                if wt not in candidate.parents:
                    raise ValueError('VERIFICATION_PATH_ESCAPE')
        log = evidence_dir / ('check-%d.log' % i)
        header = ['# verify interpreter: %s\n' % resolved_python]
        if not cfg.get('interpreter'):
            header.append('# WARN: {python} defaulted to the runner interpreter;'
                          ' set "interpreter" in control-plane.json to pin the project environment\n')
        run = _run_command(argv, wt, log, cfg.get('verify_timeout', 120), verify_env, header)
        check, signature = _classify_result(run, argv, wt, resolved_python)
        if check['exit_code'] != 0 and rerun_on:
            # Flake washout: one fresh rerun of the same command is the only
            # evidence a verdict may trust — a red that survives it is real,
            # a green rerun means the first red never described the PR.
            rerun = _run_command(argv, wt, evidence_dir / ('check-%d-rerun.log' % i),
                                 cfg.get('verify_timeout', 120), verify_env)
            rerun_check, rerun_signature = _classify_result(rerun, argv, wt, resolved_python)
            rerun_check['first_run'] = {k: check[k] for k in
                                        ('exit_code', 'log', 'sha256', 'failure_class')}
            check, signature = rerun_check, rerun_signature
        if signature and check['failure_class'] == 'env' and env_signature is None:
            env_signature = signature
        checks.append(check)
    unchanged = (git(wt, 'rev-parse', 'HEAD') == context['head']
                 and not git(wt, 'status', '--porcelain', '--untracked-files=all'))
    passed = unchanged and all(c['exit_code'] == 0 for c in checks)
    result = dict(context, passed=passed, unchanged=unchanged, checks=checks,
                  env_signature=env_signature)
    if not passed:
        attribution = _attribute_to_base(cfg, wt, checks, evidence_dir)
        if attribution:
            result['attribution'] = attribution
    return result
