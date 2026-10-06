"""Shared, file-backed state for the app.

The workspace files stay the source of truth; the store caches a DataFrame of
writeup frontmatter and reloads it whenever a writeup changes on disk, so the
app reflects agents that are working concurrently.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess

from pathlib import Path

import pandas as pd
import param

from .. import config as config_mod, core
from ..config import Config

COLUMNS = ['issue', 'title', 'category', 'confidence', 'reproduced', 'recommendation', 'verified',
           'model', 'summary', 'open', 'triaged_at', 'reviewed_by', 'corrected', 'type', 'gh_labels']


def model_of(agent: str | None) -> str:
    """Normalize an agent id like `kilo/z-ai/glm-5.3-flash@w1` to the model part."""
    return (agent or '').split('@', 1)[0]


class Store(param.Parameterized):

    df = param.DataFrame(doc='One row per writeup')

    version = param.Integer(default=0, doc='Bumped whenever the writeups change on disk')

    def __init__(self, root: Path, **params):
        super().__init__(**params)
        self.cfg: Config = config_mod.load(root)
        self._stamp = None
        self.open_numbers: set[int] = set()
        self.synced_at = None
        self.refresh(force=True)

    def reload_config(self):
        self.cfg = config_mod.load(self.cfg.root)

    def _disk_stamp(self):
        latest, count = 0.0, 0
        with os.scandir(self.cfg.issues) as it:
            for entry in it:
                if entry.name.endswith('.md'):
                    count += 1
                    latest = max(latest, entry.stat().st_mtime)
        listing = self.cfg.issue_list.stat().st_mtime if self.cfg.issue_list.exists() else 0
        return latest, count, listing

    def refresh(self, force: bool = False) -> bool:
        stamp = self._disk_stamp()
        if not force and stamp == self._stamp:
            return False
        self._stamp = stamp
        listing = core.load_issue_list(self.cfg, required=False)
        self.open_numbers = {i['number'] for i in listing['issues']}
        # Live type and labels from the synced issue list; writeups only hold what was there at triage time.
        self.issue_index = {i['number']: i for i in listing['issues']}
        self.synced_at = listing.get('synced_at')
        rows = []
        for path, meta, error in core.load_writeups(self.cfg):
            if meta is None:
                rows.append({'issue': int(path.stem), 'title': f'(unparseable: {error})', 'category': 'invalid'})
                continue
            rows.append({
                **{k: meta.get(k) for k in COLUMNS if k in meta},
                'model': model_of(meta.get('triaged_by')),
                'open': meta.get('issue') in self.open_numbers,
                'type': self.issue_index.get(meta.get('issue'), {}).get('type'),
                'gh_labels': ', '.join(self.issue_index.get(meta.get('issue'), {}).get('labels', [])),
                'reviewed_by': meta.get('verified_by') or '',
                'corrected': False,
                **{f.name: meta.get(f.name) for f in self.cfg.extra_fields},
            })
        columns = list(dict.fromkeys(COLUMNS + [f.name for f in self.cfg.extra_fields]))
        df = pd.DataFrame(rows, columns=columns)
        df = df.fillna({'verified': 'no', 'category': 'pending'})
        # A brand-new workspace has no writeups; keep the dtypes filters rely on.
        df['open'] = df['open'].fillna(False).astype(bool)
        df['issue'] = df['issue'].astype(int)
        corrected = self._corrected_issues(df)
        df['corrected'] = df['issue'].isin(corrected)
        self.df = df.sort_values('issue').reset_index(drop=True)
        self.version += 1
        return True

    def _corrected_issues(self, df) -> set[int]:
        """Writeups whose review appended a `## Verification` note, i.e. the reviewer changed something."""
        out = set()
        for n in df.loc[df['verified'].isin(['yes', 'disputed']), 'issue']:
            try:
                if '\n## Verification (' in core.writeup_path(self.cfg, int(n)).read_text():
                    out.add(int(n))
            except OSError:
                pass
        return out

    # -- per-issue files ----------------------------------------------------

    def writeup_text(self, n: int) -> str:
        return core.writeup_path(self.cfg, n).read_text()

    def save_writeup(self, n: int, text: str) -> list[str]:
        name = f'{n}.md'
        errors = core.validate_text(self.cfg, text, name, require_final=False)
        if any('frontmatter' in e or 'expected `key: value`' in e for e in errors):
            return errors
        config_mod.write_atomic(core.writeup_path(self.cfg, n), text)
        self.refresh(force=True)
        meta, _ = core.parse_text(text, name)
        final = meta.get('category') not in (None, '', 'pending')
        return core.validate_text(self.cfg, text, name, require_final=final)

    def update_fields(self, n: int, **fields) -> list[str]:
        meta, body = core.parse_writeup(core.writeup_path(self.cfg, n))
        meta.update(fields)
        return self.save_writeup(n, core.render_writeup(meta, body))

    def issue_data(self, n: int) -> dict | None:
        path = self.cfg.cache / f'{n}.json'
        return json.loads(path.read_text()) if path.exists() else None

    def repro_files(self, n: int) -> list[Path]:
        files = [Path(p) for p in glob.glob(str(self.cfg.repros / f'{n}.*'))]
        files += [Path(p) for p in glob.glob(str(self.cfg.repros / f'{n}_*'))]
        return sorted(f for f in files if not f.name.endswith('.tmp'))

    def transcripts(self, n: int) -> dict[str, Path]:
        """Agent sessions for this issue, newest first: built-in event logs and external runners' batch logs."""
        found = {p.parent.name: p for p in self.cfg.logs.glob(f'*/{n}.jsonl')}
        for job_id, runner, log in self._batch_logs().get(n, []):
            found[f'{job_id} ({runner} batch log)'] = log
        return dict(sorted(found.items(), reverse=True))

    def _batch_logs(self) -> dict[int, list]:
        stamp = max((p.stat().st_mtime for p in self.cfg.jobs.glob('*.json')), default=0)
        if getattr(self, '_batch_stamp', None) != stamp:
            index: dict[int, list] = {}
            for path in self.cfg.jobs.glob('*.json'):
                try:
                    job = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                for p in job.get('processed') or []:
                    if p.get('issue') and p.get('log'):
                        index.setdefault(p['issue'], []).append(
                            (job['id'], job.get('runner', 'external'), self.cfg.root / p['log']))
            self._batch_index, self._batch_stamp = index, stamp
        return self._batch_index

    def reviewer(self) -> str:
        name = self.cfg.agents.get('reviewer')
        if name:
            return name
        try:
            user = subprocess.run(['git', 'config', 'user.name'], capture_output=True, text=True).stdout.strip()
        except OSError:
            user = ''
        return f"human:{user or os.environ.get('USER', 'reviewer')}"

    # -- aggregates ---------------------------------------------------------

    def model_stats(self) -> pd.DataFrame:
        df = self.df[self.df['category'].isin(list(self.cfg.categories))]
        if df.empty:
            return pd.DataFrame(columns=['model', 'triaged', 'reviewed', 'as_is', 'edited', 'disputed', 'agreement'])
        g = df.groupby(df['model'].replace('', '(unknown)'))
        out = pd.DataFrame({
            'triaged': g.size(),
            'reviewed': g['verified'].apply(lambda s: s.isin(['yes', 'disputed']).sum()),
            'edited': g.apply(lambda d: (d['corrected'] & (d['verified'] == 'yes')).sum(), include_groups=False),
            'disputed': g['verified'].apply(lambda s: (s == 'disputed').sum()),
        })
        out['as_is'] = out['reviewed'] - out['edited'] - out['disputed']
        out['agreement'] = (out['as_is'] / out['reviewed'].where(out['reviewed'] > 0)).round(3)
        out = out.reset_index().rename(columns={'index': 'model'})
        return out[['model', 'triaged', 'reviewed', 'as_is', 'edited', 'disputed', 'agreement']].sort_values(
            'triaged', ascending=False)
