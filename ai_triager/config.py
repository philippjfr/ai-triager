"""Workspace discovery and configuration.

``triage.toml`` holds the project definition and is edited by hand.
``settings.json`` holds agent/LLM settings that the app edits; its values
override the ``[agents]`` table of ``triage.toml``.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tomllib

from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = 'triage.toml'
STATE_DIR = '.triage'
SETTINGS_NAME = 'settings.json'

DEFAULT_CATEGORIES = [
    ('duplicate', 'Same problem as another issue'),
    ('fixed', 'No longer reproduces on main, or the requested change exists'),
    ('obsolete', 'Refers to removed APIs, dropped platforms or superseded designs'),
    ('by-design', 'Behaviour is intentional or documented; needs an explanation, not a fix'),
    ('upstream', 'Root cause lives in a dependency, the platform or the browser'),
    ('confirmed', 'Still reproduces on main'),
    ('feature-request', 'Enhancement that is still unimplemented and still relevant'),
    ('docs', 'Documentation problem that still exists'),
    ('not-reproducible', 'Faithful attempt did not reproduce and no fix could be identified'),
    ('needs-info', 'Too little information to attempt a reproduction'),
    ('needs-human', 'Agent could not decide or could not test (hardware, auth, deployment, judgement)'),
]

DEFAULT_RULES = [
    {'when': {'category': 'duplicate'}, 'require': ['duplicate_of'],
     'message': 'duplicate requires `duplicate_of`'},
    {'when': {'category': 'fixed', 'confidence': 'high'}, 'require': ['fixed_by'],
     'message': 'high-confidence `fixed` requires `fixed_by` (PR, commit or release)'},
]

DEFAULT_DENY = [
    'gh issue comment*', 'gh issue close*', 'gh issue edit*', 'gh issue reopen*', 'gh issue create*',
    'gh issue delete*', 'gh issue transfer*', 'gh pr create*', 'gh pr comment*', 'gh pr close*',
    'gh pr edit*', 'gh pr merge*', 'gh pr review*', 'gh api * -X *', 'gh api * --method *',
    'gh api * -f *', 'gh api * -F *', '*git checkout*', '*git switch*', '*git stash*', '*git reset*',
    '*git commit*', '*git push*', '*git pull*', '*pip install*', '*pixi install*', '*pixi add*',
    'rm -rf*',
]

DEFAULT_RUNNERS = {
    'kilo': {
        'label': 'Kilo',
        'command': ['kilo', 'run', '--dir', '{root}', '--agent', '{agent}', '--model', '{model}',
                    '--title', '{title}', '--command', '{command}', '--', '{args}'],
        'models_command': ['kilo', 'models'],
    },
    'claude': {
        'label': 'Claude Code',
        'command': ['claude', '-p', '{prompt}', '--model', '{model}', '--append-system-prompt', '{system}',
                    '--permission-mode', 'acceptEdits', '--allowedTools', 'Bash', 'Read', 'Edit', 'Write',
                    'Glob', 'Grep', '--disallowedTools', '{disallowed...}', '--max-turns', '{steps}'],
        'models': ['claude-haiku-4-5', 'claude-sonnet-5-5', 'claude-opus-5-5'],
    },
}

DEFAULT_AGENTS = {
    'runner': 'kilo',
    'triage_model': '',
    'review_model': '',
    'per_batch': 5,
    'batches': 1,
    'workers': 1,
    'order': 'oldest',
    'label': '',
    'models': [],
    'deny': DEFAULT_DENY,
    'runners': DEFAULT_RUNNERS,
    'env': {},
    'reviewer': '',
}

RULE_STAGES = ('validate', 'scaffold', 'context')

MODES = {
    'triage': {'agent': 'triager', 'command': 'triage-batch', 'steps': 250,
               'done': 'nothing left to claim', 'next_flags': []},
    'review': {'agent': 'triage-reviewer', 'command': 'triage-review', 'steps': 300,
               'done': 'nothing left to review', 'next_flags': ['--review']},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass
class Field:
    name: str
    type: str = 'str'
    values: list | None = None
    default: object = None
    required: bool = False
    description: str = ''
    column: str = ''
    """Header of an INDEX.md column showing this field; empty means no column."""


@dataclass
class Config:
    root: Path
    raw: dict
    settings: dict = field(default_factory=dict)

    @property
    def project(self) -> dict:
        return self.raw.get('project', {})

    @property
    def name(self) -> str:
        return self.project.get('name') or self.repo.split('/')[-1]

    @property
    def repo(self) -> str:
        return os.environ.get('TRIAGE_REPO') or self.project.get('repo', '')

    def path(self, value: str | None) -> Path | None:
        if not value:
            return None
        p = Path(os.path.expanduser(value))
        return (p if p.is_absolute() else self.root / p).resolve()

    @property
    def checkout(self) -> Path | None:
        return self.path(os.environ.get('TRIAGE_CHECKOUT') or self.project.get('checkout'))

    @property
    def python(self) -> Path:
        value = os.environ.get('TRIAGE_PYTHON') or self.project.get('python')
        return self.path(value) if value else Path(sys.executable)

    @property
    def packages(self) -> list[str]:
        return self.project.get('packages', [])

    @property
    def package_labels(self) -> dict:
        return self.project.get('package_labels', {})

    @property
    def claim_ttl_hours(self) -> float:
        return float(os.environ.get('TRIAGE_CLAIM_TTL', self.project.get('claim_ttl_hours', 3)))

    @property
    def categories(self) -> dict[str, str]:
        cats = self.raw.get('categories')
        if cats:
            return {c['name']: c.get('description', '') for c in cats}
        return dict(DEFAULT_CATEGORIES)

    def rules(self, stage: str = 'validate') -> list[dict]:
        """Rules of one stage: `validate` checks finished writeups, `scaffold` sets fields on new ones,
        `context` adds per-component hints to `triage context`."""
        return [r for r in self.raw.get('rules', DEFAULT_RULES) if r.get('stage', 'validate') == stage]

    @property
    def skills(self) -> dict:
        """Skill directories (SKILL.md format) offered to the built-in agent."""
        return {'paths': ['skills'], 'disabled': [], **self.raw.get('skills', {}), **self.settings.get('skills', {})}

    @property
    def extra_fields(self) -> list[Field]:
        fields = self.raw.get('fields', {})
        return [Field(name=name, **spec) for name, spec in fields.items()]

    @property
    def sections(self) -> list[str]:
        return self.raw.get('writeup', {}).get(
            'sections', ['## Report', '## Reproduction', '## Diagnosis', '## Proposed GitHub comment'])

    @property
    def repro(self) -> dict:
        return {'timeout': 120, 'browse_timeout': 180, **self.raw.get('repro', {})}

    @property
    def components(self) -> dict:
        return self.raw.get('components', {})

    @property
    def agents(self) -> dict:
        merged = _deep_merge(DEFAULT_AGENTS, self.raw.get('agents', {}))
        return _deep_merge(merged, self.settings)

    def save_settings(self, settings: dict):
        self.settings = settings
        write_atomic(self.root / SETTINGS_NAME, json.dumps(settings, indent=2) + '\n')

    # What people read and edit lives at the top level; machine state lives under .triage/.
    issues = property(lambda self: self.root / 'issues')
    repros = property(lambda self: self.root / 'repros')
    prompts = property(lambda self: self.root / 'prompts')
    state = property(lambda self: self.root / STATE_DIR)
    cache = property(lambda self: self.state / 'cache')
    claims = property(lambda self: self.state / 'claims')
    logs = property(lambda self: self.state / 'logs')
    jobs = property(lambda self: self.state / 'jobs')
    data = property(lambda self: self.state)
    issue_list = property(lambda self: self.state / 'issues.json')
    env_file = property(lambda self: self.state / 'env.json')
    template = property(lambda self: self.root / 'templates' / 'issue.md')


def write_atomic(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f'.{os.getpid()}.tmp')
    tmp.write_text(text)
    os.replace(tmp, path)


def find_root(start: Path | None = None) -> Path:
    env = os.environ.get('TRIAGE_WORKSPACE')
    if env:
        return Path(env).expanduser().resolve()
    start = (start or Path.cwd()).resolve()
    for candidate in [start, *start.parents]:
        if (candidate / CONFIG_NAME).exists():
            return candidate
    raise FileNotFoundError(f'no {CONFIG_NAME} found in {start} or its parents; run `ai-triager init`')


def load(root: Path | str | None = None) -> Config:
    root = Path(root).resolve() if root else find_root()
    raw = tomllib.loads((root / CONFIG_NAME).read_text())
    for rule in raw.get('rules', []):
        if rule.get('stage', 'validate') not in RULE_STAGES:
            raise ValueError(f"{CONFIG_NAME}: rule stage {rule['stage']!r} not in {RULE_STAGES}")
    settings_path = root / SETTINGS_NAME
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    return Config(root=root, raw=raw, settings=settings)
