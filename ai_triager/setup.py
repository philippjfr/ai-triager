"""Creating workspaces, checking that they are fully configured, and editing triage.toml.

The CLI (`ai-triager init`, `ai-triager check`) and the app's onboarding wizard share this module.
Edits go through tomlkit when it is installed so comments and layout in triage.toml survive.
"""
from __future__ import annotations

import json
import sys
import os
import re
import shutil
import subprocess

from dataclasses import dataclass
from pathlib import Path

from . import credentials
from .config import CONFIG_NAME, SETTINGS_NAME, Config, write_atomic

SKELETON = Path(__file__).parent / 'skeleton'
GITHUB_REMOTE = re.compile(r'github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$')
REPO_NAME = re.compile(r'^[\w.-]+/[\w.-]+$')
WORKSPACE_DIRS = ('issues', 'repros', 'skills', 'prompts', 'templates', '.triage')


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ''


def github_repo(checkout: Path) -> str | None:
    """owner/name of the GitHub repo a checkout tracks, preferring the `upstream` remote."""
    for remote in ('upstream', 'origin'):
        match = GITHUB_REMOTE.search(git(checkout, 'remote', 'get-url', remote))
        if match:
            return match.group(1)
    return None


def display_path(path: Path, root: Path) -> str:
    """Relative to the workspace when it is nearby, absolute otherwise."""
    rel = os.path.relpath(path, root)
    return rel if rel.split(os.sep).count('..') <= 2 else str(path)


def python_candidates(checkout: Path | None) -> list[str]:
    """Interpreters worth offering for running reproducers: the checkout's pixi envs, then conda envs."""
    found = []
    if checkout:
        found += sorted(str(p) for p in checkout.glob('.pixi/envs/*/bin/python'))
        found += [str(p) for p in (checkout / '.venv' / 'bin' / 'python',) if p.exists()]
    conda = Path(os.environ.get('CONDA_EXE', '~/miniconda3/bin/conda')).expanduser().parent.parent
    found += sorted(str(p) for p in conda.glob('envs/*/bin/python'))
    return found


def create_workspace(root: Path, *, repo: str | None = None, checkout: Path | None = None, name: str | None = None,
                     python: str = '', package: str | None = None, skills: list[str] = ()) -> list[Path]:
    """Copy the skeleton into `root`, filling in what is known. Existing files are left alone."""
    root.mkdir(parents=True, exist_ok=True)
    name = name or (repo.split('/')[-1] if repo else root.name)
    skill_paths = ['skills'] + [display_path(Path(p).expanduser().resolve(), root) for p in skills]
    replacements = {
        '{{repo}}': repo or '', '{{name}}': name,
        '{{checkout}}': display_path(checkout, root) if checkout else '',
        '{{python}}': python, '{{package}}': package or name.lower().replace('-', '_'),
        '{{skills}}': json.dumps(skill_paths),
        '{{triager_path}}': str(Path(__file__).resolve().parent.parent),
        '{{triager_python}}': os.path.abspath(sys.executable),
    }
    created = []
    for src in sorted(SKELETON.rglob('*')):
        if src.is_dir() or '__pycache__' in src.parts:
            continue
        dest = root / src.relative_to(SKELETON)
        if dest.exists():
            continue
        text = src.read_text()
        for key, value in replacements.items():
            text = text.replace(key, value)
        write_atomic(dest, text)
        if dest.suffix == '.py':
            dest.chmod(0o755)
        created.append(dest.relative_to(root))
    for sub in WORKSPACE_DIRS:
        (root / sub).mkdir(exist_ok=True)
    return created


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

@dataclass
class Check:
    key: str
    label: str
    ok: bool
    detail: str
    step: str
    required: bool = True


def gh_authenticated() -> bool:
    return bool(shutil.which('gh')) and subprocess.run(['gh', 'auth', 'status'], capture_output=True).returncode == 0


def run_checks(cfg: Config, *, gh: bool = True) -> list[Check]:
    checks = []
    repo = cfg.repo
    checks.append(Check('repo', 'GitHub repository', bool(REPO_NAME.match(repo or '')),
                        repo or 'not set; use owner/name', 'repository'))
    checkout = cfg.checkout
    ok = bool(checkout and (checkout / '.git').exists())
    checks.append(Check('checkout', 'Local checkout', ok,
                        str(checkout) if ok else f'{checkout or "not set"} is not a git checkout', 'repository'))
    checks.append(Check('python', 'Reproducer interpreter', cfg.python.exists(), str(cfg.python), 'repository',
                        required=False))
    if gh:
        checks.append(Check('gh', 'GitHub CLI signed in', gh_authenticated(),
                            'ok' if gh_authenticated() else 'run `gh auth login`', 'issues'))
    synced = cfg.issue_list.exists()
    detail = 'not synced yet'
    if synced:
        listing = json.loads(cfg.issue_list.read_text())
        detail = f"{len(listing['issues'])} open issues, synced {listing.get('synced_at', '')[:10]}"
    checks.append(Check('issues', 'Issue list synced', synced, detail, 'issues'))
    agents = cfg.agents
    builtin = agents.get('runner', 'builtin') == 'builtin'
    for role, required in (('triage', True), ('review', False), ('decision', False)):
        model = agents.get(f'{role}_model') or ''
        checks.append(Check(f'{role}_model', f'{role.title()} model', bool(model), model or 'not set', 'models',
                            required=required))
        if builtin and model:
            provider = credentials.provider_of(model)
            value, where = credentials.resolve(provider, cfg)
            label = credentials.LABELS.get(provider, provider or 'unknown provider')
            if provider not in credentials.PROVIDERS and provider not in (agents.get('endpoints') or {}):
                checks.append(Check(f'{role}_key', f'Provider for {model}', False,
                                    f'unknown provider "{provider}"; add it under [agents.endpoints]', 'models',
                                    required=required))
                continue
            checks.append(Check(f'{role}_key', f'API key for {label}', value is not None,
                                where or f'no key found for {label}', 'models', required=required))
    return checks


def incomplete(cfg: Config, *, gh: bool = False) -> list[Check]:
    return [c for c in run_checks(cfg, gh=gh) if c.required and not c.ok]


# ---------------------------------------------------------------------------
# Editing triage.toml
# ---------------------------------------------------------------------------

def array_of_tables(items: list[dict]):
    """An array of tables whose nested dicts stay inline, e.g. ``when = { category = "fixed" }``."""
    import tomlkit

    aot = tomlkit.aot()
    for item in items:
        table = tomlkit.table()
        for key, value in item.items():
            if isinstance(value, dict):
                inline = tomlkit.inline_table()
                inline.update(value)
                value = inline
            table[key] = value
        aot.append(table)
    return aot


def update_config(root: Path, values: dict[str, object]):
    """Set dotted keys (``project.repo``, ``agents.keys.openai``) in triage.toml, preserving comments.

    Lists of dicts become arrays of tables (``categories``, ``rules``); ``None`` removes a key. Agent
    settings written here are dropped from settings.json so they are not shadowed by an older value.
    """
    import tomlkit

    path = root / CONFIG_NAME
    doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    for dotted, value in values.items():
        *parents, key = dotted.split('.')
        table = doc
        for part in parents:
            if part not in table:
                table[part] = tomlkit.table()
            table = table[part]
        if value is None:
            table.pop(key, None)
        elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            table[key] = array_of_tables(value)
        else:
            table[key] = value
    write_atomic(path, tomlkit.dumps(doc))

    settings_path = root / SETTINGS_NAME
    if settings_path.exists():
        settings = json.loads(settings_path.read_text())
        changed = False
        for dotted in values:
            section, _, key = dotted.partition('.')
            top = key.split('.')[0]
            if section == 'agents' and top in settings:
                settings.pop(top)
                changed = True
            elif section == 'skills' and 'skills' in settings:
                settings.pop('skills')
                changed = True
        if changed:
            write_atomic(settings_path, json.dumps(settings, indent=2) + '\n')
