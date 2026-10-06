"""Reviewing close recommendations and acting on them.

A maintainer goes through the open issues that writeups recommend closing and records a decision
in each writeup's frontmatter: ``accept`` (post the proposed comment and close), ``rereview`` (send
it back to a reviewer agent with a note) or ``keep`` (leave the issue open). Acting on decisions is
explicit and separate: re-reviews reset the writeup's verification so a review job picks it up, and
closing posts the comment with ``gh issue close``. That is the only place ai-triager writes to
GitHub, and only when a person asks for it.
"""
from __future__ import annotations

import re
import subprocess

from . import core
from .config import Config, write_atomic

DECISIONS = ('accept', 'rereview', 'keep')
COMMENT_HEADING = '## Proposed GitHub comment'
VERIFICATION = re.compile(r'^## Verification \(([^,]+), ([^)]+)\)', re.M)
CLOSE_REASONS = {'duplicate': 'duplicate', 'fixed': 'completed'}


def proposed_comment(body: str) -> str:
    if COMMENT_HEADING not in body:
        return ''
    rest = body.split(COMMENT_HEADING, 1)[1]
    return re.split(r'^## ', rest, maxsplit=1, flags=re.M)[0].strip()


def replace_comment(body: str, comment: str) -> str:
    if COMMENT_HEADING not in body:
        return body.rstrip() + f'\n\n{COMMENT_HEADING}\n\n{comment.strip()}\n'
    head, rest = body.split(COMMENT_HEADING, 1)
    parts = re.split(r'(^## )', rest, maxsplit=1, flags=re.M)
    tail = ''.join(parts[1:]) if len(parts) > 1 else ''
    return f'{head}{COMMENT_HEADING}\n\n{comment.strip()}\n\n{tail}'.rstrip() + '\n'


def reviewers(meta: dict, body: str) -> list[dict]:
    """Who looked at a writeup: the triaging agent, then each recorded verification."""
    out = [{'role': 'triaged', 'by': meta.get('triaged_by') or 'unknown', 'when': str(meta.get('triaged_at') or '')}]
    out += [{'role': 'reviewed', 'by': by, 'when': when} for by, when in VERIFICATION.findall(body)]
    if meta.get('verified') in ('yes', 'disputed') and not VERIFICATION.findall(body):
        out.append({'role': 'reviewed', 'by': meta.get('verified_by') or 'unknown', 'when': ''})
    return out


def candidates(cfg: Config) -> list[dict]:
    """Open issues whose writeup recommends closing, with everything needed to decide."""
    listing = {i['number']: i for i in core.load_issue_list(cfg, required=False)['issues']}
    rows = []
    for path, meta, _ in core.load_writeups(cfg):
        if not meta or meta.get('recommendation') != 'close' or meta.get('issue') not in listing:
            continue
        if meta.get('decision') == 'closed':
            continue
        _, body = core.parse_writeup(path)
        dup = str(meta.get('duplicate_of') or '').lstrip('#')
        target = listing.get(int(dup)) if dup.isdigit() else None
        rows.append({
            'issue': meta['issue'], 'title': meta.get('title'), 'category': meta.get('category'),
            'confidence': meta.get('confidence'), 'verified': meta.get('verified') or 'no',
            'verified_by': meta.get('verified_by') or '', 'decision': meta.get('decision') or '',
            'decision_note': meta.get('decision_note') or '', 'summary': meta.get('summary') or '',
            'duplicate_of': f'#{dup}' if dup else '', 'duplicate_title': target['title'] if target else '',
            'duplicate_open': bool(target), 'fixed_by': meta.get('fixed_by') or [],
            'comment': proposed_comment(body), 'reviewers': reviewers(meta, body),
        })
    return rows


def set_decision(cfg: Config, n: int, decision: str | None, by: str, note: str = '', comment: str | None = None):
    path = core.writeup_path(cfg, n)
    meta, body = core.parse_writeup(path)
    meta['decision'] = decision
    meta['decision_by'] = by if decision else None
    meta['decision_note'] = note or None
    if comment is not None and comment.strip() != proposed_comment(body):
        body = replace_comment(body, comment)
    write_atomic(path, core.render_writeup(meta, body))


def request_rereview(cfg: Config, numbers: list[int], by: str) -> list[int]:
    """Reset verification so the next review job picks these up, keeping the maintainer's note for it."""
    done = []
    for n in numbers:
        path = core.writeup_path(cfg, n)
        meta, body = core.parse_writeup(path)
        note = meta.get('decision_note') or 'No note given.'
        body = body.rstrip() + f'\n\n## Re-review requested ({by}, {core.now().date()})\n\n{note}\n'
        meta.update(verified='no', verified_by=None, decision='rereview-requested')
        write_atomic(path, core.render_writeup(meta, body))
        done.append(n)
    return done


def close_command(cfg: Config, row: dict) -> list[str]:
    reason = CLOSE_REASONS.get(row['category'], 'not planned')
    cmd = ['gh', 'issue', 'close', str(row['issue']), '--repo', cfg.repo, '--reason', reason]
    if row['comment']:
        cmd += ['--comment', row['comment']]
    if reason == 'duplicate' and row['duplicate_of']:
        cmd += ['--duplicate-of', row['duplicate_of'].lstrip('#')]
    return cmd


def close_issue(cfg: Config, row: dict, by: str, dry_run: bool = False) -> dict:
    """Post the comment and close the issue; records the outcome in the writeup."""
    cmd = close_command(cfg, row)
    if dry_run:
        return {'issue': row['issue'], 'ok': True, 'output': 'dry run: ' + ' '.join(cmd[:8])}
    proc = subprocess.run(cmd, capture_output=True, text=True)
    ok = proc.returncode == 0
    if ok:
        path = core.writeup_path(cfg, row['issue'])
        meta, body = core.parse_writeup(path)
        meta.update(decision='closed', decision_by=by, closed_at=core.now().isoformat())
        body = body.rstrip() + f"\n\n## Closed on GitHub ({by}, {core.now().date()})\n\n{row['comment']}\n"
        write_atomic(path, core.render_writeup(meta, body))
    return {'issue': row['issue'], 'ok': ok, 'output': (proc.stdout + proc.stderr).strip()[:500]}
