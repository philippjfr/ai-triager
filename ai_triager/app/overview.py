from __future__ import annotations

import json

import pandas as pd
import panel as pn
import param

from .. import core, jobs
from . import charts
from .store import Store
from .theme import KPI, section


def _chart(**params) -> pn.ui.Vega:
    return pn.ui.Vega(sizing_mode='stretch_width', margin=0, **params)


class Overview(pn.viewable.Viewer):
    """Headline numbers, charts of the triage state, active claims and validation errors."""

    store = param.ClassSelector(class_=Store)

    def __init__(self, on_open_category, on_open_issue, **params):
        super().__init__(**params)
        self._on_open_category = on_open_category
        self._on_open_issue = on_open_issue
        self._kpis = {name: KPI(name) for name in
                      ('Open issues', 'Triaged', 'Untouched', 'Verified', 'Recommend close', 'Agent spend')}
        self._categories = _chart(debounce=0)
        self._progress = _chart()
        self._recommendations = _chart()
        self._review = _chart()
        self._types = _chart()
        # Shown only when the repository uses GitHub issue types.
        self._types_section = section(
            'Issue types', self._types, visible=False,
            subtitle='Open issues by their GitHub issue type and whether they have been triaged.')
        self._claims = pn.ui.Column(sizing_mode='stretch_width', margin=0)
        self._errors = pn.ui.Column(sizing_mode='stretch_width', margin=0)
        self._layout = pn.ui.Grid(
            *(pn.ui.Grid(k.card, size={'xs': 6, 'sm': 4, 'lg': 2}) for k in self._kpis.values()),
            pn.ui.Grid(
                self._stack(
                    section('Categories', self._categories,
                            subtitle='Finished writeups by category. Click a bar to browse them.'),
                    self._types_section,
                    section('Review status by model', self._review,
                            subtitle='Reviewed writeups split by whether the reviewer had to change them.'),
                ),
                size={'xs': 12, 'lg': 6},
            ),
            pn.ui.Grid(
                self._stack(
                    section('Triage progress', self._progress,
                            subtitle='Finished writeups over time, by the day they were triaged.'),
                    section('Recommendations', self._recommendations,
                            subtitle='What the agents recommend, by how confident they were.'),
                    section('Active claims', self._claims),
                ),
                size={'xs': 12, 'lg': 6},
            ),
            pn.ui.Grid(section('Validation errors', self._errors), size={'xs': 12}),
            container=True, spacing=2, sizing_mode='stretch_width', margin=10,
        )
        self._update()
        # The pane creates its `pick` selection parameter from the spec, so watch it after the first update.
        self._categories.selection.param.watch(self._on_pick, 'pick')
        self.store.param.watch(self._update, 'version')

    def __panel__(self):
        return self._layout

    @staticmethod
    def _stack(*sections) -> pn.ui.Column:
        return pn.ui.Column(*sections, sizing_mode='stretch_width', margin=0, sx={'gap': '16px'})

    def _update(self, *events):
        store, cfg = self.store, self.store.cfg
        df = store.df
        final = df[df['category'].isin(list(cfg.categories))]
        n_open = len(store.open_numbers)
        triaged_open = int(final['open'].sum())
        pending = int((df['category'] == 'pending').sum())
        verified = int((df['verified'] == 'yes').sum())
        disputed = int((df['verified'] == 'disputed').sum())
        closable = int(((df['recommendation'] == 'close') & df['open']).sum())
        spend = sum((j.get('totals') or {}).get('cost') or 0 for j in jobs.list_jobs(cfg))
        k = self._kpis
        with pn.io.hold():
            k['Open issues'].update(f'{n_open:,}', f'synced {(store.synced_at or "never")[:10]}')
            k['Triaged'].update(f'{len(final):,}', f'{triaged_open / n_open:.0%} of open issues' if n_open else '')
            k['Untouched'].update(f'{max(n_open - triaged_open - pending, 0):,}', f'{pending} in progress')
            k['Verified'].update(f'{verified:,}', f'{disputed} disputed')
            k['Recommend close'].update(f'{closable:,}', 'still open upstream')
            k['Agent spend'].update(f'${spend:,.2f}', 'built-in runner')
            self._set(self._categories, charts.categories(final['category'].value_counts(), cfg.categories))
            self._set(self._progress, charts.progress(final['triaged_at']))
            self._set(self._recommendations, charts.recommendations(final))
            self._set(self._review, charts.review_status(store.model_stats()))
            triaged = set(final['issue'])
            types = [{'type': store.issue_index.get(n, {}).get('type'), 'triaged': n in triaged}
                     for n in store.open_numbers]
            self._types_section.visible = any(t['type'] for t in types)
            if self._types_section.visible:
                self._set(self._types, charts.issue_types(types))
            self._claims.objects = self._claim_rows()
            self._errors.objects = self._error_rows(final)

    def _set(self, pane: pn.ui.Vega, spec: dict):
        # Only replace the spec when the data changed, so charts don't redraw on every poll.
        if json.dumps(pane.object, sort_keys=True, default=str) == json.dumps(spec, sort_keys=True, default=str):
            return
        if pane is self._categories and pane.object is not None:
            # Panel's Vega pane resets existing selections to None on a new spec, which List-typed point
            # selections reject; start from a fresh Selection and re-attach the click handler instead.
            pane.selection = None
            pane.object = spec
            pane.selection.param.watch(self._on_pick, 'pick')
        else:
            pane.object = spec

    def _on_pick(self, event):
        picked = [v['category'] for v in event.new or [] if isinstance(v, dict) and 'category' in v]
        if picked:
            self._on_open_category(picked[0])

    def _claim_rows(self) -> list:
        claims = core.active_claims(self.store.cfg)
        if not claims:
            return [pn.ui.Typography('No issues are claimed right now.', variant='body2', margin=0)]
        rows = []
        for name, claim in claims:
            release = pn.ui.Button(label='Release', size='small', variant='outlined', margin=0)
            release.on_click(lambda e, n=name: self._release(n))
            rows.append(pn.ui.Row(
                pn.ui.Typography(f"{name} by {claim['agent']} since {claim['claimed_at'][11:16]} UTC",
                                 variant='body2', margin=0, sizing_mode='stretch_width'),
                release, align='center', sizing_mode='stretch_width', margin=0,
            ))
        return rows

    def _error_rows(self, final: pd.DataFrame) -> list:
        cfg = self.store.cfg
        errors = []
        for n in final['issue']:
            errors += core.validate_writeup(cfg, core.writeup_path(cfg, int(n)))
        if not errors:
            return [pn.ui.Typography('All finished writeups validate.', variant='body2', margin=0)]
        rows = []
        for err in errors[:50]:
            n = err.split('.md', 1)[0]
            link = pn.ui.Button(label=f'#{n}', size='small', variant='text', width=80, margin=0,
                                sx={'justifyContent': 'flex-start'})
            link.on_click(lambda e, n=int(n): self._on_open_issue(n))
            rows.append(pn.ui.Row(
                link, pn.ui.Typography(err.split(': ', 1)[-1], variant='body2', margin=0, sizing_mode='stretch_width'),
                align='center', sizing_mode='stretch_width', margin=0,
            ))
        return rows

    def _release(self, name: str):
        core.release(self.store.cfg, int(name.removeprefix('review-')), review=name.startswith('review-'))
        self._update()
