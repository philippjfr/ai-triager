"""Where agent shells and reproducers execute.

Issue text is untrusted input that an agent reads and may be steered by, so everything an agent
executes runs in a sandbox configured under ``[sandbox]``:

- ``seatbelt`` (macOS ``sandbox-exec``): host tools and environments, but no outbound network except
  localhost, credential files unreadable, and writes limited to the workspace's writable paths and
  temporary directories;
- ``docker``: a container with the workspace mounted, the checkout read-only and networking off;
  the image must provide the project's environment;
- ``local``: no isolation, for trusted setups only.

Reproducers only see an allowlisted environment (``DEFAULT_ENV`` plus ``[sandbox] env``), so API keys
and tokens exported in the shell or loaded by ai-triager never reach them. ``allow_all = true`` turns
everything off: no sandbox, no guard and the full environment, for repositories you trust.

``probe()`` tries what a hostile reproducer would and reports whether each attempt was blocked.
"""
from __future__ import annotations

import fnmatch
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile

from dataclasses import dataclass
from pathlib import Path

from .config import Config

SECRET_PATHS = ['~/.ssh', '~/.gnupg', '~/.aws', '~/.azure', '~/.config/gcloud', '~/.config/gh', '~/.config/ai-triager',
                '~/.netrc', '~/.pypirc', '~/.docker', '~/.kube', '~/.git-credentials', '~/Library/Keychains']
# Reproducers write their own outputs; agent shells also run `./triage.py finish`, which updates the index
# and claims under .triage. Review agents may refine AGENTS.md.
# Environment variables reproducers may see; everything else (API keys, tokens, cloud credentials) is dropped.
DEFAULT_ENV = ['PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TERM', 'TMPDIR', 'TZ', 'LANG', 'LC_*',
               'PLAYWRIGHT_BROWSERS_PATH', 'TRIAGE_*']
DEFAULT_WRITABLE = {'reproducer': ['repros'], 'agent': ['issues', 'repros', 'NOTES.md', 'INDEX.md', '.triage']}


@dataclass
class SandboxSpec:
    backend: str
    network: bool
    writable: list[Path]
    secrets: list[Path]
    image: str
    root: Path
    checkout: Path | None


def allow_all(cfg: Config) -> bool:
    """Trusted mode: no sandbox, no guard, full environment."""
    return bool(cfg.raw.get('sandbox', {}).get('allow_all', False))


def environment(cfg: Config, extra: dict | None = None, base: dict | None = None) -> dict:
    """The environment for a reproducer: allowlisted variables from ``base`` (the process env) plus ``extra``."""
    base = dict(os.environ if base is None else base)
    if allow_all(cfg):
        return {**base, **(extra or {})}
    patterns = DEFAULT_ENV + list(cfg.raw.get('sandbox', {}).get('env', []))
    kept = {k: v for k, v in base.items() if any(fnmatch.fnmatchcase(k, p) for p in patterns)}
    return {**kept, **(extra or {})}


def default_backend() -> str:
    if platform.system() == 'Darwin' and shutil.which('sandbox-exec'):
        return 'seatbelt'
    if shutil.which('docker'):
        return 'docker'
    return 'local'


def spec(cfg: Config, kind: str = 'reproducer') -> SandboxSpec:
    raw = cfg.raw.get('sandbox', {})
    section = {**raw, **raw.get(kind, {})}
    root = cfg.root
    writable = [cfg.path(p) for p in section.get('writable', DEFAULT_WRITABLE.get(kind, ['repros']))]
    backend = 'local' if allow_all(cfg) else (
        os.environ.get('TRIAGE_SANDBOX') or section.get('backend') or default_backend())
    return SandboxSpec(
        backend=backend,
        network=bool(section.get('network', False)),
        writable=[p for p in writable if p],
        secrets=[Path(os.path.expanduser(p)) for p in section.get('secrets', SECRET_PATHS)],
        image=section.get('image', ''),
        root=root,
        checkout=cfg.checkout,
    )


def _sbpl_path(path: Path) -> str:
    return str(path.resolve()).replace('\\', '\\\\').replace('"', '\\"')


def seatbelt_profile(s: SandboxSpec) -> str:
    rules = ['(version 1)', '(allow default)']
    if not s.network:
        rules += ['(deny network-outbound (remote ip "*:*"))',
                  '(allow network-outbound (remote ip "localhost:*"))',
                  '(allow network-outbound (remote unix-socket))']
    if s.secrets:
        rules.append('(deny file-read* ' + ' '.join(f'(subpath "{_sbpl_path(p)}")' for p in s.secrets) + ')')
    writable = [*s.writable, Path(tempfile.gettempdir()), Path('/private/tmp'), Path('/private/var/folders')]
    allowed = ' '.join(f'(subpath "{_sbpl_path(p)}")' for p in writable)
    rules += ['(deny file-write*)', f'(allow file-write* {allowed} (literal "/dev/null") (literal "/dev/tty") '
              '(regex #"^/dev/fd/") (regex #"^/dev/ttys"))']
    return ''.join(rules)


def wrap(cmd: list[str], s: SandboxSpec, cwd: Path) -> tuple[list[str], dict]:
    """The command line to run ``cmd`` inside the sandbox, plus environment overrides."""
    env = {'TRIAGE_SANDBOX_BACKEND': s.backend}
    if s.backend == 'seatbelt':
        return ['sandbox-exec', '-p', seatbelt_profile(s), *cmd], env
    if s.backend == 'docker':
        if not s.image:
            raise ValueError('[sandbox] backend = "docker" needs an image with the project environment')
        mounts = ['-v', f'{s.root}:{s.root}:ro']
        mounts += [x for p in s.writable if p.exists() for x in ('-v', f'{p}:{p}')]
        if s.checkout:
            mounts += ['-v', f'{s.checkout}:{s.checkout}:ro']
        network = [] if s.network else ['--network', 'none']
        return ['docker', 'run', '--rm', '-i', *network, *mounts, '-w', str(cwd), s.image, *cmd], env
    return cmd, env


def run(cmd: list[str], cfg: Config, *, kind: str = 'reproducer', cwd: Path | None = None, **kwargs):
    s = spec(cfg, kind)
    wrapped, env = wrap(cmd, s, cwd or cfg.root)
    return subprocess.run(wrapped, cwd=cwd or cfg.root, env=environment(cfg, {**env, **kwargs.pop('env', {})}),
                          **kwargs)


def shell_command(command: str, cfg: Config, kind: str = 'agent') -> str:
    """A shell command string that runs ``command`` in the sandbox (for the agent's bash tool)."""
    s = spec(cfg, kind)
    wrapped, env = wrap(['/bin/sh', '-c', command], s, cfg.root)
    prefix = ' '.join(f'{k}={shlex.quote(v)}' for k, v in env.items())
    return f'{prefix} {shlex.join(wrapped)}'


PROBES = [
    ('read SSH keys', 'ls ~/.ssh', False),
    ('read the GitHub CLI token', 'cat ~/.config/gh/hosts.yml', False),
    ('see API keys in the environment', 'test -n "$OPENAI_API_KEY$AI_TRIAGER_PROBE_TOKEN"', False),
    ('reach the internet', 'curl -s -m 5 -o /dev/null https://example.com', False),
    ('write to the home directory', 'touch ~/.ai_triager_probe', False),
    ('modify the project checkout', 'touch {checkout}/.ai_triager_probe', False),
    ('write a reproducer', 'touch {root}/repros/.ai_triager_probe && rm {root}/repros/.ai_triager_probe', True),
    ('run Python', '{python} -c "print(1)"', True),
]


def probe(cfg: Config, kind: str = 'reproducer') -> list[dict]:
    """Try what a hostile reproducer would; each result says whether the sandbox behaved as expected."""
    results = []
    s = spec(cfg, kind)
    for label, command, should_work in PROBES:
        command = command.format(root=cfg.root, checkout=cfg.checkout or cfg.root, python=cfg.python or sys.executable)
        wrapped, env = wrap(['/bin/sh', '-c', command], s, cfg.root)
        # A planted token checks that secrets in the caller's environment are filtered out.
        probe_env = environment(cfg, env, base={**os.environ, 'AI_TRIAGER_PROBE_TOKEN': 'probe'})
        try:
            proc = subprocess.run(wrapped, capture_output=True, text=True, timeout=30, cwd=cfg.root,
                                  env=probe_env)
            worked = proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError, ValueError):
            worked = False
        results.append({'check': label, 'allowed': worked, 'expected': should_work, 'ok': worked == should_work})
    for leftover in (Path.home() / '.ai_triager_probe', (cfg.checkout or cfg.root) / '.ai_triager_probe'):
        leftover.unlink(missing_ok=True)
    return results
