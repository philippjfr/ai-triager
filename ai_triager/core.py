"""Writeups, claims, environment info and the generated index.

Stdlib only, so the CLI runs with any Python >= 3.11.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess

from pathlib import Path

from .config import Config, write_atomic

ENUMS = {
    'confidence': {'low', 'medium', 'high'},
    'reproduced': {'yes', 'no', 'partial', 'untested', 'n/a'},
    'recommendation': {'close', 'keep', 'needs-info', 'relabel', 'escalate'},
    'verified': {'no', 'yes', 'disputed'},
}
REQUIRED_FINAL = ['category', 'confidence', 'reproduced', 'recommendation', 'summary', 'triaged_by', 'tested_on']
LIST_FIELDS = ['labels', 'components', 'fixed_by', 'related']
SCALAR_FIELDS = ['duplicate_of', 'repro', 'summary', 'triaged_by', 'verified_by']
REVIEW_ORDER = {'close': 0, 'needs-info': 1, 'relabel': 2, 'escalate': 3, 'keep': 4}
SIMPLE_TOKEN = re.compile(r'[A-Za-z0-9._/+-]+')


class TriageError(Exception):
    pass


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def run(cmd: list[str], **kwargs) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if proc.returncode:
        raise TriageError(f'{" ".join(cmd)} failed:\n{proc.stderr.strip()}')
    return proc.stdout


# ---------------------------------------------------------------------------
# Issue list, component map and environment
# ---------------------------------------------------------------------------

def load_issue_list(cfg: Config, required: bool = True) -> dict:
    if not cfg.issue_list.exists():
        if required:
            raise TriageError('no issue list, run `./triage.py sync` first')
        return {'issues': [], 'synced_at': None}
    return json.loads(cfg.issue_list.read_text())


def load_component_map(cfg: Config) -> dict:
    path = cfg.path(cfg.components.get('map'))
    return json.loads(path.read_text()) if path and path.exists() else {}


def detect_components(cfg: Config, text: str, cmap: dict) -> list[str]:
    spec = cfg.components
    generic_names = set(spec.get('generic', []))
    max_len = spec.get('generic_max_len', 4)
    qualifiers = '|'.join(map(re.escape, spec.get('qualifiers', [])))
    found = [name for name in cmap if re.search(rf'\b{re.escape(name)}\b', text)]
    # Short or generic names only count when qualified, e.g. pn.Row.
    generic = {n for n in found if len(n) <= max_len or n in generic_names}
    return sorted(
        n for n in found
        if n not in generic or (qualifiers and re.search(rf'({qualifiers})\.{n}\b', text))
    )


def env_info(cfg: Config) -> dict:
    """Package versions and commit of the checkout reproducers run against, cached per HEAD."""
    checkout = cfg.checkout
    head = run(['git', '-C', str(checkout), 'rev-parse', '--short', 'HEAD']).strip() if checkout else ''
    if cfg.env_file.exists():
        info = json.loads(cfg.env_file.read_text())
        if info.get('head') == head and info.get('packages') == cfg.packages:
            return info
    code = (
        'import json, sys, importlib\n'
        'out = {"python": sys.version.split()[0]}\n'
        f'for name in {cfg.packages!r}:\n'
        '    try:\n'
        '        mod = importlib.import_module(name)\n'
        '        out[name] = getattr(mod, "__version__", "?")\n'
        '    except Exception:\n'
        '        out[name] = None\n'
        'print(json.dumps(out))'
    )
    versions = json.loads(run([str(cfg.python), '-c', code]))
    dirty = bool(checkout) and bool(run(
        ['git', '-C', str(checkout), 'status', '--porcelain', '--untracked-files=no']).strip())
    info = {'versions': versions, 'packages': cfg.packages, 'head': head, 'dirty': dirty}
    write_atomic(cfg.env_file, json.dumps(info, indent=2))
    return info


def tested_on(cfg: Config, info: dict) -> str:
    versions = info.get('versions', {})
    parts = []
    for i, name in enumerate(cfg.packages):
        label = cfg.package_labels.get(name, name)
        part = f'{label} {versions.get(name)}'
        if i == 0 and info.get('head'):
            part += f" ({info['head']}{'+dirty' if info.get('dirty') else ''})"
        parts.append(part)
    return ', '.join(parts) or f"python {versions.get('python')}"


# ---------------------------------------------------------------------------
# Writeup files
# ---------------------------------------------------------------------------

def writeup_path(cfg: Config, n: int) -> Path:
    return cfg.issues / f'{n}.md'


def parse_text(text: str, name: str = 'writeup') -> tuple[dict, str]:
    if not text.startswith('---\n') or '\n---\n' not in text[3:]:
        raise ValueError(f'{name}: missing frontmatter block')
    end = text.index('\n---\n', 3)
    meta = {}
    for lineno, line in enumerate(text[4:end].splitlines(), 2):
        if not line.strip():
            continue
        key, sep, raw = line.partition(':')
        if not sep:
            raise ValueError(f'{name}:{lineno}: expected `key: value`')
        raw = raw.strip()
        try:
            value = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            value = raw
        meta[key.strip()] = value
    return meta, text[end + 5:]


def parse_writeup(path: Path) -> tuple[dict, str]:
    return parse_text(path.read_text(), path.name)


def dump_value(value) -> str:
    if value is None:
        return ''
    if isinstance(value, str) and SIMPLE_TOKEN.fullmatch(value) and value not in ('true', 'false', 'null'):
        return value
    return json.dumps(value, ensure_ascii=False)


def render_writeup(meta: dict, body: str) -> str:
    lines = ['---'] + [f'{k}: {dump_value(v)}'.rstrip() for k, v in meta.items()] + ['---']
    return '\n'.join(lines) + '\n' + body


class _Blank(dict):
    def __missing__(self, key):
        return ''


def _fill(value, values: dict):
    return value.format_map(_Blank(values)) if isinstance(value, str) else value


def _matches(meta: dict, when: dict, component: dict | None = None) -> bool:
    """Whether a rule's `when` table holds.

    Plain keys compare frontmatter fields with a value or a list of allowed values. Keys of the
    form `component.<attr>` test an entry of the component map: `true`/`false` check that the
    attribute is set/unset, anything else compares like a field.
    """
    for key, expected in when.items():
        if key.startswith('component.'):
            if component is None:
                return False
            value = component.get(key.removeprefix('component.'))
            if isinstance(expected, bool):
                if bool(value) != expected:
                    return False
                continue
        else:
            value = meta.get(key)
        if value not in (expected if isinstance(expected, list) else [expected]):
            return False
    return True


def _uses_components(rule: dict) -> bool:
    return any(k.startswith('component.') for k in rule.get('when', {}))


def component_info(cmap: dict, name: str) -> dict:
    return {'name': name, **cmap.get(name, {})}


def apply_scaffold_rules(cfg: Config, meta: dict, cmap: dict):
    """Set fields on a new writeup; component rules fire for the first matching component."""
    for rule in cfg.rules('scaffold'):
        when = rule.get('when', {})
        if _uses_components(rule):
            hits = [component_info(cmap, c) for c in meta.get('components', [])
                    if _matches(meta, when, component_info(cmap, c))]
            if not hits:
                continue
            values = {**meta, **hits[0]}
        elif _matches(meta, when):
            values = meta
        else:
            continue
        for key, value in rule.get('set', {}).items():
            meta[key] = _fill(value, values)


def component_lines(cfg: Config, components: list[str], cmap: dict) -> list[str]:
    """One line per detected component with the hints of matching `context` rules."""
    display = cfg.components.get('display', '{name}')
    lines = []
    for name in components:
        info = component_info(cmap, name)
        hints = [_fill(r['hint'], info) for r in cfg.rules('context')
                 if r.get('hint') and _matches({}, r.get('when', {}), info)]
        lines.append(f"- `{_fill(display, info)}`" + (f": {'; '.join(hints)}" if hints else ''))
    return lines


def validate_text(cfg: Config, text: str, name: str, require_final: bool = True) -> list[str]:
    try:
        meta, body = parse_text(text, name)
    except ValueError as e:
        return [str(e)]
    errors = []
    stem = name.removesuffix('.md')
    if stem.isdigit() and meta.get('issue') != int(stem):
        errors.append(f'{name}: `issue` must be {stem}')
    enums = dict(ENUMS, category=set(cfg.categories) | {'pending'})
    for f in cfg.extra_fields:
        if f.type == 'enum':
            enums[f.name] = set(f.values or [])
    for key, allowed in enums.items():
        value = meta.get(key)
        if value not in (None, '') and value not in allowed:
            errors.append(f'{name}: `{key}: {value}` not in {sorted(allowed)}')
    scalars = SCALAR_FIELDS + [f.name for f in cfg.extra_fields if f.type in ('str', 'enum')]
    for key in scalars:
        if isinstance(meta.get(key), (list, dict)):
            errors.append(f'{name}: `{key}` must be a single value, not a list')
    lists = LIST_FIELDS + [f.name for f in cfg.extra_fields if f.type == 'list']
    for key in lists:
        if key in meta and not isinstance(meta[key], list):
            errors.append(f'{name}: `{key}` must be a JSON list, e.g. ["#1234"]')
    if not require_final:
        return errors
    if meta.get('category') in (None, '', 'pending'):
        errors.append(f'{name}: `category` is still pending')
    for key in REQUIRED_FINAL:
        if meta.get(key) in (None, '', []):
            errors.append(f'{name}: `{key}` is required')
    for f in cfg.extra_fields:
        if f.type == 'bool' and f.required and meta.get(f.name) not in (True, False):
            errors.append(f'{name}: `{f.name}` must be true or false')
        elif f.required and f.type != 'bool' and meta.get(f.name) in (None, '', []):
            errors.append(f'{name}: `{f.name}` is required')
    for rule in cfg.rules('validate'):
        if not _matches(meta, rule.get('when', {})):
            continue
        missing = [k for k in rule.get('require', []) if not meta.get(k)]
        bad = {k: v for k, v in rule.get('expect', {}).items() if meta.get(k) not in v}
        if missing or bad:
            errors.append(f"{name}: {rule.get('message') or f'rule {rule} violated'}")
    if meta.get('reproduced') in ('yes', 'no', 'partial'):
        repro = meta.get('repro')
        if not repro:
            errors.append(f'{name}: `reproduced: {meta.get("reproduced")}` requires `repro` path')
        elif not (cfg.root / repro).exists():
            errors.append(f'{name}: repro file {repro} does not exist')
    for section in cfg.sections:
        if section not in body:
            errors.append(f'{name}: missing section `{section}`')
    if 'TODO' in body:
        errors.append(f'{name}: body still contains TODO placeholders')
    if len(meta.get('summary') or '') > 200:
        errors.append(f'{name}: `summary` longer than 200 characters')
    return errors


def validate_writeup(cfg: Config, path: Path, require_final: bool = True) -> list[str]:
    return validate_text(cfg, path.read_text(), path.name, require_final)


def scaffold(cfg: Config, issue: dict, cmap: dict) -> Path:
    path = writeup_path(cfg, issue['number'])
    if path.exists():
        return path
    body = cfg.template.read_text().split('\n---\n', 1)[1]
    comps = detect_components(cfg, f"{issue['title']}\n{issue.get('body') or ''}", cmap)
    extra = {f.name: copy_default(f.default) for f in cfg.extra_fields}
    meta = {
        'issue': issue['number'],
        'title': issue['title'],
        'url': f"https://github.com/{cfg.repo}/issues/{issue['number']}",
        'opened': issue['created_at'][:10],
        'labels': issue['labels'],
        'category': 'pending',
        'confidence': None,
        'reproduced': 'untested',
        'repro': None,
        'tested_on': None,
        'components': comps,
        **extra,
        'fixed_by': [],
        'duplicate_of': None,
        'related': [],
        'recommendation': None,
        'summary': None,
        'triaged_by': None,
        'triaged_at': None,
        'verified': 'no',
        'verified_by': None,
    }
    apply_scaffold_rules(cfg, meta, cmap)
    write_atomic(path, render_writeup(meta, body))
    return path


def copy_default(value):
    return list(value) if isinstance(value, list) else value


def load_writeups(cfg: Config) -> list[tuple[Path, dict | None, str | None]]:
    """Every writeup as ``(path, meta, error)``, sorted by issue number."""
    out = []
    for path in sorted(cfg.issues.glob('*.md'), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
        try:
            out.append((path, parse_writeup(path)[0], None))
        except ValueError as e:
            out.append((path, None, str(e)))
    return out


def set_verdict(cfg: Config, n: int, result: str, agent: str, note: str | None = None):
    path = writeup_path(cfg, n)
    meta, body = parse_writeup(path)
    meta['verified'] = result
    meta['verified_by'] = agent
    if note:
        body = body.rstrip() + f'\n\n## Verification ({agent}, {now().date()})\n\n{note}\n'
    write_atomic(path, render_writeup(meta, body))
    release(cfg, n, review=True)


def finish(cfg: Config, n: int, agent: str | None) -> tuple[dict, list[str]]:
    path = writeup_path(cfg, n)
    if not path.exists():
        return {}, [f'#{n}: no writeup']
    meta, body = parse_writeup(path)
    meta['triaged_at'] = meta.get('triaged_at') or now().date().isoformat()
    if agent and not meta.get('triaged_by'):
        meta['triaged_by'] = agent
    if not meta.get('tested_on') and meta.get('reproduced') not in ('untested', 'n/a'):
        meta['tested_on'] = tested_on(cfg, env_info(cfg))
    if not meta.get('tested_on'):
        meta['tested_on'] = 'not run'
    write_atomic(path, render_writeup(meta, body))
    errors = validate_writeup(cfg, path)
    if not errors:
        release(cfg, n)
    return meta, errors


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------

def claim_path(cfg: Config, n: int, review: bool = False) -> Path:
    return cfg.claims / (f'review-{n}' if review else f'{n}')


def active_claim(cfg: Config, n: int, review: bool = False) -> dict | None:
    path = claim_path(cfg, n, review)
    if not path.exists():
        return None
    try:
        claim = json.loads(path.read_text())
        claimed = dt.datetime.fromisoformat(claim['claimed_at'])
    except (ValueError, KeyError):
        return None
    return claim if now() - claimed < dt.timedelta(hours=cfg.claim_ttl_hours) else None


def try_claim(cfg: Config, n: int, agent: str, review: bool = False) -> bool:
    cfg.claims.mkdir(exist_ok=True)
    path = claim_path(cfg, n, review)
    if path.exists():
        if active_claim(cfg, n, review):
            return False
        path.unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, 'w') as f:
        json.dump({'agent': agent, 'claimed_at': now().isoformat()}, f)
    return True


def release(cfg: Config, n: int, review: bool = False):
    claim_path(cfg, n, review).unlink(missing_ok=True)


def release_agent(cfg: Config, agent: str):
    for path in cfg.claims.glob('*') if cfg.claims.exists() else []:
        try:
            if json.loads(path.read_text()).get('agent') == agent:
                path.unlink()
        except (ValueError, OSError):
            pass


def active_claims(cfg: Config) -> list[tuple[str, dict]]:
    claims = []
    for path in sorted(cfg.claims.glob('*')) if cfg.claims.exists() else []:
        review = path.name.startswith('review-')
        stem = path.name.removeprefix('review-')
        if not stem.isdigit():
            continue
        claim = active_claim(cfg, int(stem), review)
        if claim:
            claims.append((path.name, claim))
    return claims


# ---------------------------------------------------------------------------
# Selecting work
# ---------------------------------------------------------------------------

def candidates(cfg: Config, order: str = 'oldest', label: str | None = None,
               issues: list[int] | None = None, exclude: set[int] = frozenset()) -> list[dict]:
    import random

    listing = [i for i in load_issue_list(cfg)['issues'] if i['number'] not in exclude]
    if issues:
        wanted = set(issues)
        return [i for i in listing if i['number'] in wanted]
    if label:
        listing = [i for i in listing if any(label.lower() in lbl.lower() for lbl in i['labels'])]
    if order == 'oldest':
        listing.sort(key=lambda i: i['number'])
    elif order == 'newest':
        listing.sort(key=lambda i: -i['number'])
    elif order == 'stale':
        listing.sort(key=lambda i: i['updated_at'])
    else:
        random.shuffle(listing)
    return listing


def claim_next(cfg: Config, n: int, agent: str, order: str = 'oldest', label: str | None = None,
               issues: list[int] | None = None, dry_run: bool = False,
               exclude: set[int] = frozenset()) -> list[dict]:
    cmap = load_component_map(cfg)
    picked = []
    for issue in candidates(cfg, order, label, issues, exclude):
        if len(picked) >= n:
            break
        num = issue['number']
        path = writeup_path(cfg, num)
        if path.exists() and not issues:
            try:
                if parse_writeup(path)[0].get('category') not in (None, '', 'pending'):
                    continue
            except ValueError:
                pass
        if dry_run:
            if not active_claim(cfg, num):
                picked.append(issue)
            continue
        if not try_claim(cfg, num, agent):
            continue
        scaffold(cfg, issue, cmap)
        picked.append(issue)
    return picked


def claim_review(cfg: Config, n: int, agent: str, dry_run: bool = False,
                 issues: list[int] | None = None, exclude: set[int] = frozenset()) -> list[dict]:
    rows = []
    wanted = set(issues or [])
    for _, meta, _ in load_writeups(cfg):
        if not meta or (wanted and meta.get('issue') not in wanted) or meta.get('issue') in exclude:
            continue
        if meta.get('category') in cfg.categories and meta.get('verified', 'no') == 'no':
            rows.append((REVIEW_ORDER.get(meta.get('recommendation'), 9), meta['issue'], meta))
    picked = []
    for _, num, meta in sorted(rows, key=lambda r: r[:2]):
        if len(picked) >= n:
            break
        if dry_run:
            if not active_claim(cfg, num, review=True):
                picked.append(meta)
        elif try_claim(cfg, num, agent, review=True):
            picked.append(meta)
    return picked


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def _md_cell(text) -> str:
    return str(text or '').replace('|', '\\|').replace('\n', ' ')


def _column_cell(field, value) -> str:
    if field.type == 'bool':
        return 'yes' if value else ''
    return _md_cell(', '.join(map(str, value)) if isinstance(value, list) else value)


def build_index(cfg: Config) -> dict:
    listing = load_issue_list(cfg, required=False)
    open_numbers = {i['number'] for i in listing['issues']}
    rows, errors = [], []
    for path, meta, error in load_writeups(cfg):
        if error:
            errors.append(error)
            continue
        if meta.get('category') in cfg.categories:
            errors += validate_writeup(cfg, path)
        meta['open'] = meta.get('issue') in open_numbers
        rows.append(meta)
    counts = {c: 0 for c in cfg.categories}
    for row in rows:
        if row.get('category') in counts:
            counts[row['category']] += 1
    done = sum(counts.values())
    pending = sum(1 for r in rows if r.get('category') == 'pending')
    verified = sum(1 for r in rows if r.get('verified') == 'yes')
    closable = sum(1 for r in rows if r.get('recommendation') == 'close' and r['open'])
    untouched = len(open_numbers) - done - pending
    claims = active_claims(cfg)
    columns = [f for f in cfg.extra_fields if f.column]

    out = [
        f'# {cfg.name} issue triage index',
        '',
        '<!-- Generated by `./triage.py index`. Do not edit by hand; edit issues/<N>.md instead. -->',
        '',
        f"Synced {len(open_numbers)} open issues at {listing.get('synced_at')}. "
        f'Triaged {done}, in progress {pending}, untouched {untouched}. '
        f'Verified {verified}. Recommended to close: {closable}.',
        '',
        '| Category | Count | Meaning |',
        '| --- | --- | --- |',
    ]
    out += [f'| [{c}](#{c}) | {counts[c]} | {desc} |' for c, desc in cfg.categories.items()]
    if claims:
        out += ['', '## Active claims', '']
        out += [f"- `{name}` by {c['agent']} since {c['claimed_at']}" for name, c in claims]
    if errors:
        out += ['', '## Validation errors', ''] + [f'- {e}' for e in errors]
    extra_header = ''.join(f' {f.column} |' for f in columns)
    for cat in cfg.categories:
        items = [r for r in rows if r.get('category') == cat]
        if not items:
            continue
        out += ['', f'## {cat}', '', f'| # | Title | Conf | Repro |{extra_header} Rec | Verified | Summary |',
                '| --- | --- | --- | --- |' + ' --- |' * len(columns) + ' --- | --- | --- |']
        for r in items:
            closed = '' if r['open'] else ' (closed)'
            extra_cells = ''.join(f' {_column_cell(f, r.get(f.name))} |' for f in columns)
            out.append(
                f"| [#{r['issue']}](issues/{r['issue']}.md){closed} | {_md_cell(r.get('title'))[:80]} "
                f"| {r.get('confidence') or ''} | {r.get('reproduced') or ''} |{extra_cells}"
                f" {r.get('recommendation') or ''} | {r.get('verified') or 'no'} | {_md_cell(r.get('summary'))} |"
            )
    write_atomic(cfg.root / 'INDEX.md', '\n'.join(out) + '\n')
    index = {'synced_at': listing.get('synced_at'), 'counts': counts, 'pending': pending,
             'untouched': untouched, 'verified': verified, 'closable': closable,
             'errors': errors, 'issues': rows}
    write_atomic(cfg.data / 'index.json', json.dumps(index, indent=1, ensure_ascii=False, default=str))
    return index
