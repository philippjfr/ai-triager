"""Label review: does each issue carry the right labels?

The repository's label catalogue is synced with ``gh label list``. A decision model (any model
string, ``[agents] decision_model``, custom endpoints included) judges each current label and
suggests missing ones, using only labels from the catalogue. Results live in
``.triage/label-review.json`` until a maintainer applies or dismisses them; applying runs
``gh issue edit`` and is the only step that writes to GitHub.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time

from typing import Literal

from pydantic import BaseModel, Field

from . import core, decisions, github
from .config import Config, write_atomic

STATUSES = ('pending', 'applied', 'dismissed', 'ok')


class LabelJudgement(BaseModel):
    label: str = Field(description='A label currently on the issue')
    keep: bool = Field(description='Whether the label fits the issue')
    reason: str = Field(description='One short sentence')


class LabelSuggestion(BaseModel):
    label: str = Field(description='A label from the catalogue that the issue is missing')
    reason: str = Field(description='One short sentence')


class LabelVerdict(BaseModel):
    current: list[LabelJudgement] = Field(description='One judgement per label currently on the issue')
    add: list[LabelSuggestion] = Field(description='Catalogue labels to add; empty if none are clearly missing')
    issue_type: str | None = Field(default=None, description='The issue type from the type catalogue that fits '
                                                              'best, or null if there is no type catalogue')
    confidence: Literal['low', 'medium', 'high']


# ---------------------------------------------------------------------------
# Catalogue and stored results
# ---------------------------------------------------------------------------

def catalogue_path(cfg: Config):
    return cfg.state / 'labels.json'


def results_path(cfg: Config):
    return cfg.state / 'label-review.json'


def fetch_issue_types(cfg: Config) -> list[dict]:
    """The organisation's issue types available in the repository (empty when it has none)."""
    owner, _, name = cfg.repo.partition('/')
    query = ('query($owner:String!,$name:String!){repository(owner:$owner,name:$name)'
             '{issueTypes(first:50){nodes{name description}}}}')
    try:
        out = core.run(['gh', 'api', 'graphql', '-f', f'query={query}', '-F', f'owner={owner}', '-F', f'name={name}',
                        '--jq', '.data.repository.issueTypes.nodes // []'])
    except core.TriageError:
        return []
    return json.loads(out or '[]')


def sync_catalogue(cfg: Config) -> list[dict]:
    out = core.run(['gh', 'label', 'list', '--repo', cfg.repo, '--limit', '1000', '--json', 'name,description'])
    labels = sorted(json.loads(out), key=lambda label: label['name'].lower())
    write_atomic(catalogue_path(cfg), json.dumps({'synced_at': core.now().isoformat(), 'labels': labels,
                                                  'issue_types': fetch_issue_types(cfg)}, indent=1))
    return labels


def _catalogue_file(cfg: Config) -> dict:
    try:
        return json.loads(catalogue_path(cfg).read_text())
    except (OSError, ValueError):
        return {}


def catalogue(cfg: Config, *, include_excluded: bool = False) -> list[dict]:
    """Labels the reviewer may judge; `[labels] exclude` hides ones such as Dependabot's PR labels."""
    labels = _catalogue_file(cfg).get('labels', [])
    if include_excluded:
        return labels
    excluded = set(cfg.raw.get('labels', {}).get('exclude', []))
    return [label for label in labels if label['name'] not in excluded]


def issue_types(cfg: Config) -> list[dict]:
    return _catalogue_file(cfg).get('issue_types', [])


def results(cfg: Config) -> dict[int, dict]:
    """Stored reviews, reconciled with GitHub as last synced.

    Suggestions for labels that left the catalogue are dropped, as are removals and kept labels the issue
    no longer carries (renamed or deleted labels, or changes made on GitHub since the review).
    """
    try:
        data = {int(k): v for k, v in json.loads(results_path(cfg).read_text()).items()}
    except (OSError, ValueError):
        return {}
    known = {label['name'] for label in catalogue(cfg)}
    if not known:
        return data
    listing = core.load_issue_list(cfg, required=False).get('issues', [])
    current = {i['number']: set(i.get('labels', [])) for i in listing}
    types = {i['number']: i.get('type') for i in listing}
    for n, r in data.items():
        on_issue = current.get(n)
        if on_issue is not None:
            r['current'] = [c for c in r.get('current', []) if c['label'] in on_issue]
        if r.get('status') != 'pending':
            continue
        r['add'] = [a for a in r.get('add', []) if a['label'] in known and a['label'] not in (on_issue or ())]
        if on_issue is not None:
            r['remove'] = [x for x in r.get('remove', []) if x in on_issue]
        kind = r.get('type') or {}
        if kind.get('change') and n in types and types[n] == kind.get('suggested'):
            kind['change'] = False
        if not (r['add'] or r.get('remove') or kind.get('change')):
            r['status'] = 'ok'
    return data


def save_results(cfg: Config, data: dict[int, dict]):
    write_atomic(results_path(cfg), json.dumps({str(k): v for k, v in sorted(data.items())}, indent=1))


def update_result(cfg: Config, n: int, **fields):
    data = results(cfg)
    data.setdefault(n, {}).update(fields)
    save_results(cfg, data)


# ---------------------------------------------------------------------------
# Choosing issues and classifying them
# ---------------------------------------------------------------------------

def select_issues(cfg: Config, scope: str = 'unreviewed', issues: list[int] | None = None,
                  limit: int | None = None) -> list[dict]:
    listing = core.load_issue_list(cfg)['issues']
    if issues:
        wanted = set(issues)
        chosen = [i for i in listing if i['number'] in wanted]
    else:
        done = results(cfg)
        triaged = {m['issue'] for _, m, _ in core.load_writeups(cfg) if m}
        chosen = {
            'unreviewed': [i for i in listing if i['number'] not in done],
            'unlabelled': [i for i in listing if not i['labels']],
            'triaged': [i for i in listing if i['number'] in triaged and i['number'] not in done],
            'all': listing,
        }[scope]
    chosen = sorted(chosen, key=lambda i: i['number'])
    return chosen[:limit] if limit else chosen


def instructions(cfg: Config, labels: list[dict], types: list[dict] = ()) -> str:
    lines = '\n'.join(f"- {label['name']}: {label.get('description') or '(no description)'}" for label in labels)
    text = (
        f'You review the labels on GitHub issues of {cfg.repo}. Judge every label currently on the issue '
        '(keep it or not) and suggest labels that are clearly missing. Only use labels from this catalogue, '
        'spelled exactly as shown. Be conservative: suggest a label only when the issue clearly calls for it, '
        'and prefer an empty list to a guess.\n\nLabel catalogue:\n' + lines
    )
    if types:
        text += ('\n\nAlso choose the issue type that fits best, from this catalogue:\n'
                 + '\n'.join(f"- {t['name']}: {t.get('description') or ''}" for t in types))
    return text


def issue_prompt(cfg: Config, issue: dict) -> str:
    parts = [f"Issue #{issue['number']}: {issue['title']}",
             f"Current labels: {', '.join(issue['labels']) or 'none'}",
             f"Current issue type: {issue.get('type') or 'none'}", '', (issue.get('body') or '')[:4000]]
    path = core.writeup_path(cfg, issue['number'])
    if path.exists():
        try:
            meta, _ = core.parse_writeup(path)
            if meta.get('category') not in (None, '', 'pending'):
                parts += ['', f"A triage writeup classified it as `{meta['category']}`: {meta.get('summary') or ''}"]
        except ValueError:
            pass
    return '\n'.join(parts)


def build_agent(cfg: Config, model: str, labels: list[dict], types: list[dict] = ()):
    from pydantic_ai import Agent

    from . import models

    settings = {'anthropic_cache': True} if model.startswith(('anthropic:', 'gateway/anthropic:')) else None
    return Agent(models.resolve(model, cfg), name='label-review', output_type=LabelVerdict,
                 instructions=instructions(cfg, labels, types), model_settings=settings, retries=2)


def type_result(issue: dict, suggested: str | None, p: float | None = None, probabilities: dict | None = None,
                add_at: float = 0.0) -> dict | None:
    """Suggest a type change only when the best type differs from the current one and clears the threshold."""
    if not suggested:
        return None
    current = issue.get('type')
    change = suggested != current and (p is None or p >= add_at)
    return {'current': current, 'suggested': suggested, 'p': p, 'probabilities': probabilities or {},
            'change': change}


def normalize(verdict: LabelVerdict, issue: dict, known: set[str], types: set[str] = frozenset()) -> dict:
    """Keep only catalogue labels and make sure every current label has a judgement."""
    judged = {j.label: j for j in verdict.current}
    current = [{'label': label, 'keep': judged[label].keep if label in judged else True,
                'reason': judged[label].reason if label in judged else 'Not judged; kept.'}
               for label in issue['labels']]
    add = [{'label': s.label, 'reason': s.reason} for s in verdict.add
           if s.label in known and s.label not in issue['labels']]
    remove = [c['label'] for c in current if not c['keep']]
    kind = type_result(issue, verdict.issue_type if verdict.issue_type in types else None)
    changed = add or remove or (kind and kind['change'])
    return {'labels': issue['labels'], 'current': current, 'add': add, 'remove': remove, 'type': kind,
            'confidence': verdict.confidence, 'status': 'pending' if changed else 'ok'}


def thresholds(cfg: Config) -> tuple[float, float]:
    """Probability above which a missing label is suggested, and below which a current one is."""
    spec = cfg.raw.get('labels', {})
    return float(spec.get('add_threshold', 0.75)), float(spec.get('remove_threshold', 0.25))


def decision_state(cfg: Config, issue: dict) -> dict:
    state = {'repository': cfg.repo, 'title': issue['title'], 'body': (issue.get('body') or '')[:4000],
             'current_labels': issue['labels'], 'current_type': issue.get('type')}
    path = core.writeup_path(cfg, issue['number'])
    if path.exists():
        try:
            meta, _ = core.parse_writeup(path)
        except ValueError:
            meta = {}
        if meta.get('category') not in (None, '', 'pending'):
            state['triage'] = {'category': meta['category'], 'summary': meta.get('summary')}
    return state


TYPE_QUESTION = 'issue_type'


def decision_questions(labels: list[dict], types: list[dict] = ()) -> tuple[dict[str, dict], dict[str, str]]:
    """One yes/no question per catalogue label and one choice among the issue types.

    Returns the questions and question-name -> label.
    """
    questions, names = {}, {}
    for i, label in enumerate(labels):
        key = f'label_{i}'
        names[key] = label['name']
        desc = f" It means: {label['description']}" if label.get('description') else ''
        questions[key] = decisions.noul(f'Should this GitHub issue carry the label "{label["name"]}"?{desc}')
    if types:
        questions[TYPE_QUESTION] = {'type': 'choice', 'instructions': 'Which issue type fits this GitHub issue best?',
                                    'criteria': {t['name']: t.get('description') or t['name'] for t in types}}
    return questions, names


def from_probabilities(issue: dict, probs: dict[str, float], add_at: float, remove_at: float,
                       kind: dict | None = None) -> dict:
    current = [{'label': label, 'keep': probs.get(label, 1.0) >= remove_at, 'p': probs.get(label),
                'reason': f'{probs[label]:.0%} likely to apply' if label in probs else 'Not in the catalogue; kept.'}
               for label in issue['labels']]
    add = [{'label': label, 'p': p, 'reason': f'{p:.0%} likely to apply'}
           for label, p in sorted(probs.items(), key=lambda kv: -kv[1])
           if p >= add_at and label not in issue['labels']]
    remove = [c['label'] for c in current if not c['keep']]
    # Confidence reflects how far the changes clear their thresholds.
    margins = [a['p'] for a in add] + [1 - probs[r] for r in remove]
    if kind and kind['change'] and kind['p'] is not None:
        margins.append(kind['p'])
    confidence = 'high' if not margins or min(margins) >= 0.85 else 'medium' if min(margins) >= 0.7 else 'low'
    changed = add or remove or (kind and kind['change'])
    return {'labels': issue['labels'], 'current': current, 'add': add, 'remove': remove, 'type': kind,
            'confidence': confidence, 'probabilities': probs, 'status': 'pending' if changed else 'ok'}


async def classify(cfg: Config, issues: list[dict], model: str, *, workers: int = 4, on_result=None) -> dict:
    if not catalogue(cfg, include_excluded=True):
        sync_catalogue(cfg)
    labels = catalogue(cfg)
    types = issue_types(cfg)
    known = {label['name'] for label in labels}
    if decisions.is_decision_model(model):
        return await _classify_decisions(cfg, issues, model, labels, types, workers=workers, on_result=on_result)
    agent = build_agent(cfg, model, labels, types)
    type_names = {t['name'] for t in types}
    semaphore = asyncio.Semaphore(max(1, workers))
    totals = {'cost': 0.0, 'done': 0, 'failed': 0}

    async def one(issue: dict):
        async with semaphore:
            start = time.monotonic()
            try:
                run = await agent.run(issue_prompt(cfg, issue))
            except Exception as e:
                totals['failed'] += 1
                entry = {'issue': issue['number'], 'status': 'error', 'error': f'{type(e).__name__}: {e}'[:300]}
            else:
                usage = run.usage
                cost = float(usage.cost) if getattr(usage, 'cost', None) is not None else None
                totals['cost'] += cost or 0
                totals['done'] += 1
                entry = {'issue': issue['number'], **normalize(run.output, issue, known, type_names), 'model': model,
                         'at': core.now().isoformat(), 'cost': cost}
                update_result(cfg, issue['number'], **{k: v for k, v in entry.items() if k != 'issue'})
            entry['duration'] = round(time.monotonic() - start, 1)
            if on_result:
                on_result(entry)

    await asyncio.gather(*(one(issue) for issue in issues))
    return totals


async def _classify_decisions(cfg: Config, issues: list[dict], model: str, labels: list[dict], types: list[dict], *,
                              workers: int, on_result=None) -> dict:
    questions, names = decision_questions(labels, types)
    add_at, remove_at = thresholds(cfg)
    semaphore = asyncio.Semaphore(max(1, workers))
    totals = {'cost': 0.0, 'done': 0, 'failed': 0}

    async def one(issue: dict):
        async with semaphore:
            start = time.monotonic()
            try:
                result = await decisions.invoke(model, decision_state(cfg, issue), questions, cfg)
            except Exception as e:
                totals['failed'] += 1
                entry = {'issue': issue['number'], 'status': 'error', 'error': f'{type(e).__name__}: {e}'[:300]}
            else:
                answers = result['answers']
                probs = {names[k]: round(float(a['noul']), 3) for k, a in answers.items() if k in names}
                kind = None
                if TYPE_QUESTION in answers:
                    choice = answers[TYPE_QUESTION]
                    type_probs = {k: round(float(v), 3) for k, v in (choice.get('probabilities') or {}).items()}
                    kind = type_result(issue, choice.get('choice'), type_probs.get(choice.get('choice')),
                                       type_probs, add_at)
                usage = result.get('usage') or {}
                cost = usage.get('cost')
                totals['cost'] += cost or 0
                totals['done'] += 1
                entry = {'issue': issue['number'], **from_probabilities(issue, probs, add_at, remove_at, kind),
                         'model': f"{model} ({result.get('model')})" if result.get('model') else model,
                         'at': core.now().isoformat(), 'cost': cost, 'usage': usage}
                update_result(cfg, issue['number'], **{k: v for k, v in entry.items() if k != 'issue'})
            entry['duration'] = round(time.monotonic() - start, 1)
            if on_result:
                on_result(entry)

    await asyncio.gather(*(one(issue) for issue in issues))
    return totals


def apply(cfg: Config, n: int, add: list[str], remove: list[str], issue_type: str | None = None,
          dry_run: bool = False) -> dict:
    cmd = ['gh', 'issue', 'edit', str(n), '--repo', cfg.repo]
    for label in add:
        cmd += ['--add-label', label]
    for label in remove:
        cmd += ['--remove-label', label]
    if issue_type:
        cmd += ['--type', issue_type]
    if dry_run or not (add or remove or issue_type):
        return {'issue': n, 'ok': True, 'output': 'dry run: ' + ' '.join(cmd)}
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        update_result(cfg, n, status='applied', applied_add=add, applied_remove=remove, applied_type=issue_type,
                      applied_at=core.now().isoformat())
    return {'issue': n, 'ok': proc.returncode == 0, 'output': (proc.stdout + proc.stderr).strip()[:300]}


def refresh_issue_list_labels(cfg: Config):
    """Pick up label changes made on GitHub (ours included) by re-syncing the issue list."""
    return github.sync(cfg)
