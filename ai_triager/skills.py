"""Agent skills: directories with a ``SKILL.md`` (YAML frontmatter with ``name`` and
``description``, then markdown instructions), the format Claude Code and other agents use.

The built-in runner exposes each enabled skill as a deferred pydantic-ai capability: the
model sees the catalog of names and descriptions and calls ``load_capability`` to read a
skill's instructions, then reads any bundled files with ``read_file``.
"""
from __future__ import annotations

import fnmatch
import re

from dataclasses import dataclass
from pathlib import Path

from .config import Config

SKILL_FILE = 'SKILL.md'


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str
    source: str

    @property
    def id(self) -> str:
        return re.sub(r'[^A-Za-z0-9_-]+', '-', self.name).strip('-') or 'skill'


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith('---'):
        return {}, text
    end = text.find('\n---', 3)
    if end < 0:
        return {}, text
    raw, body = text[3:end], text[end + 4:].lstrip('\n')
    try:
        import yaml
        meta = yaml.safe_load(raw) or {}
    except ImportError:
        meta = {}
        for line in raw.splitlines():
            key, sep, value = line.partition(':')
            if sep and not line.startswith((' ', '\t')):
                meta[key.strip()] = value.strip().strip('"\'')
    except Exception:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), body


def load_skill(path: Path, source: str) -> Skill | None:
    file = path / SKILL_FILE
    try:
        meta, body = _frontmatter(file.read_text())
    except OSError:
        return None
    name = str(meta.get('name') or path.name)
    description = ' '.join(str(meta.get('description') or '').split())
    return Skill(name=name, description=description, path=path.resolve(), body=body, source=source)


def discover(cfg: Config) -> list[Skill]:
    """All skills under the configured paths; a path is a skill directory or a directory of them."""
    found: dict[str, Skill] = {}
    for entry in cfg.skills.get('paths', []):
        root = cfg.path(entry)
        if not root or not root.is_dir():
            continue
        candidates = [root] if (root / SKILL_FILE).exists() else sorted(
            p for p in root.iterdir() if (p / SKILL_FILE).exists())
        for path in candidates:
            skill = load_skill(path, entry)
            if skill and skill.id not in found:
                found[skill.id] = skill
    return list(found.values())


def enabled(cfg: Config, mode: str | None = None) -> list[Skill]:
    disabled = set(cfg.skills.get('disabled', []))
    patterns = cfg.agents.get('modes', {}).get(mode, {}).get('skills', ['*']) if mode else ['*']
    return [s for s in discover(cfg)
            if s.name not in disabled and any(fnmatch.fnmatchcase(s.name, p) for p in patterns)]


def instructions(skill: Skill) -> str:
    return (
        f'# Skill: {skill.name}\n\n'
        f'This skill lives in `{skill.path}`. Relative paths below are relative to that directory; '
        f'read the files they point to with `read_file` and an absolute path when you need them.\n\n'
        f'{skill.body}'
    )


def capabilities(cfg: Config, mode: str) -> list:
    from pydantic_ai.capabilities import Capability

    return [
        Capability(id=s.id, description=s.description or None, instructions=instructions(s), defer_loading=True)
        for s in enabled(cfg, mode)
    ]
