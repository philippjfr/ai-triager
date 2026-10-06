"""Read-only GitHub access through the ``gh`` CLI."""
from __future__ import annotations

import json
import subprocess

from . import core
from .config import Config, write_atomic


def gh_lines(path: str, jq: str) -> list:
    """Paginated gh api call returning one decoded JSON value per output line."""
    out = core.run(['gh', 'api', '--paginate', path, '--jq', jq])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def sync(cfg: Config) -> dict:
    """Fetch the open issue list and record what changed since the previous sync."""
    jq = (
        '.[] | select(.pull_request | not) | {number, title, state, created_at, updated_at, comments, body,'
        ' labels: [.labels[].name], type: (.type.name // null), user: .user.login,'
        ' reactions: .reactions.total_count}'
    )
    previous = {i['number'] for i in core.load_issue_list(cfg, required=False)['issues']}
    issues = gh_lines(f'repos/{cfg.repo}/issues?state=open&per_page=100', jq)
    limit = int(cfg.project.get('issue_body_limit', 4000))
    for issue in issues:
        issue['body'] = (issue.get('body') or '')[:limit]
    synced_at = core.now().isoformat()
    payload = {'synced_at': synced_at, 'repo': cfg.repo, 'issues': sorted(issues, key=lambda i: i['number'])}
    write_atomic(cfg.issue_list, json.dumps(payload, indent=1, ensure_ascii=False))
    current = {i['number'] for i in issues}
    summary = {'synced_at': synced_at, 'total': len(current),
               'new': sorted(current - previous) if previous else [], 'closed': sorted(previous - current)}
    history = sync_history(cfg)
    history.append(summary)
    write_atomic(cfg.state / 'sync.json', json.dumps(history[-50:], indent=1))
    return summary


def sync_history(cfg: Config) -> list[dict]:
    path = cfg.state / 'sync.json'
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def stale(cfg: Config) -> list[dict]:
    """Finished writeups whose open issue saw activity after the day it was triaged."""
    listing = {i['number']: i for i in core.load_issue_list(cfg, required=False)['issues']}
    out = []
    for _, meta, _ in core.load_writeups(cfg):
        if not meta or meta.get('category') in (None, '', 'pending'):
            continue
        issue = listing.get(meta.get('issue'))
        triaged = str(meta.get('triaged_at') or '')
        if issue and triaged and issue['updated_at'][:10] > triaged:
            out.append({'issue': issue['number'], 'title': issue['title'], 'category': meta.get('category'),
                        'triaged_at': triaged, 'updated_at': issue['updated_at'][:10],
                        'comments': issue.get('comments', 0)})
    return sorted(out, key=lambda r: r['updated_at'], reverse=True)


def refresh_details(cfg: Config, numbers: list[int], progress=None) -> list[str]:
    """Re-fetch body, comments and timeline for these issues into the cache; returns errors."""
    errors = []
    for i, n in enumerate(numbers, 1):
        try:
            fetch_issue(cfg, n, refresh=True)
        except core.TriageError as e:
            errors.append(f'#{n}: {e}')
        if progress:
            progress(i, len(numbers))
    return errors


def fetch_issue(cfg: Config, n: int, refresh: bool = False) -> dict:
    path = cfg.cache / f'{n}.json'
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    repo = cfg.repo
    issue = json.loads(core.run(['gh', 'api', f'repos/{repo}/issues/{n}']))
    comments = gh_lines(
        f'repos/{repo}/issues/{n}/comments?per_page=100',
        '.[] | {user: .user.login, created_at, body, association: .author_association}',
    )
    timeline = gh_lines(
        f'repos/{repo}/issues/{n}/timeline?per_page=100',
        '.[] | select(.event == "cross-referenced" or .event == "referenced" or .event == "closed"'
        ' or .event == "reopened" or .event == "marked_as_duplicate" or .event == "connected")'
        ' | {event, created_at, commit_id, actor: .actor.login,'
        ' source: (if .source.issue then {number: .source.issue.number, title: .source.issue.title,'
        ' state: .source.issue.state, repo: .source.issue.repository.full_name, url: .source.issue.html_url,'
        ' is_pr: (.source.issue.pull_request != null), merged_at: .source.issue.pull_request.merged_at}'
        ' else null end)}',
    )
    data = {
        'number': n,
        'title': issue['title'],
        'state': issue['state'],
        'user': issue['user']['login'],
        'created_at': issue['created_at'],
        'updated_at': issue['updated_at'],
        'labels': [label['name'] for label in issue['labels']],
        'milestone': (issue.get('milestone') or {}).get('title'),
        'body': issue.get('body') or '',
        'comments': comments,
        'timeline': timeline,
        'fetched_at': core.now().isoformat(),
    }
    write_atomic(path, json.dumps(data, indent=1, ensure_ascii=False))
    return data


def _truncate(text: str, limit: int) -> str:
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f'\n\n[... truncated {len(text) - limit} chars, full text in .triage/cache/]'


def timeline_refs(cfg: Config, data: dict) -> list[str]:
    refs = []
    for ev in data['timeline']:
        src = ev.get('source')
        if ev['event'] == 'cross-referenced' and src:
            kind = 'PR' if src['is_pr'] else 'issue'
            status = 'merged ' + src['merged_at'][:10] if src.get('merged_at') else src['state']
            repo = '' if src['repo'] == cfg.repo else src['repo']
            refs.append(f"- {kind} {repo}#{src['number']} [{status}]: {src['title']}")
        elif ev['event'] == 'referenced' and ev.get('commit_id'):
            refs.append(f"- commit {ev['commit_id'][:10]} on {ev['created_at'][:10]}")
        elif ev['event'] in ('closed', 'reopened', 'marked_as_duplicate'):
            refs.append(f"- {ev['event']} by @{ev.get('actor')} on {ev['created_at'][:10]}")
    return refs


def context(cfg: Config, n: int, *, refresh: bool = False, limit: int = 12000, comment_limit: int = 3000,
            with_env: bool = True) -> str:
    data = fetch_issue(cfg, n, refresh)
    out = [
        f"# #{n}: {data['title']}",
        '',
        f"- URL: https://github.com/{cfg.repo}/issues/{n}",
        f"- State: {data['state']}, opened {data['created_at'][:10]} by @{data['user']},"
        f" updated {data['updated_at'][:10]}",
        f"- Labels: {', '.join(data['labels']) or 'none'}",
    ]
    if data.get('milestone'):
        out.append(f"- Milestone: {data['milestone']}")
    if with_env:
        out.append(f'- Test against: {core.tested_on(cfg, core.env_info(cfg))}')
    out += ['', '## Body', '', _truncate(data['body'], limit), '']
    comments = data['comments']
    if comments:
        out.append(f'## Comments ({len(comments)})')
        for c in comments:
            out += ['', f"### @{c['user']} ({c['association'].lower()}) on {c['created_at'][:10]}", '',
                    _truncate(c['body'], comment_limit)]
        out.append('')
    out.append('## Timeline references')
    out += timeline_refs(cfg, data) or ['- none']
    if cfg.checkout:
        log = subprocess.run(
            ['git', '-C', str(cfg.checkout), 'log', '--oneline', '-E', f'--grep=#{n}([^0-9]|$)', 'HEAD'],
            capture_output=True, text=True,
        ).stdout.strip()
        out += ['', f'## {cfg.name} commits mentioning this issue', log or 'none']
    cmap = core.load_component_map(cfg)
    comps = core.detect_components(cfg, f"{data['title']}\n{data['body']}", cmap)
    if comps:
        out += ['', '## Components mentioned', *core.component_lines(cfg, comps, cmap)]
    out += ['', f'Writeup: issues/{n}.md  Repro: repros/{n}.py  (app: repros/{n}_app.py)']
    return '\n'.join(out)
