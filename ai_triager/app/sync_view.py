"""Keeping the issue list and cached issue details in step with GitHub."""
from __future__ import annotations

import asyncio

import pandas as pd
import panel as pn
import param

from .. import core, github
from .store import Store
from .theme import held, section


class SyncView(pn.viewable.Viewer):

    store = param.ClassSelector(class_=Store)

    def __init__(self, notify, on_open_issue, **params):
        super().__init__(**params)
        self._notify = notify
        self._on_open_issue = on_open_issue
        self._status = pn.ui.Typography('', variant='body2', margin=0)
        self._sync_btn = pn.ui.Button(label='Sync issue list', icon='sync', color='primary')
        self._result = pn.ui.Column(sizing_mode='stretch_width', margin=0)
        self._history = pn.ui.Tabulator(show_index=False, disabled=True, sizing_mode='stretch_width', height=220,
                                        theme='materialize', layout='fit_columns', margin=0)
        self._stale = pn.ui.Tabulator(show_index=False, disabled=True, selectable=1, sizing_mode='stretch_width',
                                      height=300, theme='materialize', layout='fit_columns', margin=0,
                                      widths={'issue': 80, 'title': 360, 'comments': 100},
                                      formatters={'issue': {'type': 'plaintext'}, 'title': {'type': 'textarea'}})
        self._refresh_stale = pn.ui.Button(label='Refresh their details', icon='download', variant='outlined')
        self._refresh_all = pn.ui.Button(label='Refresh every triaged open issue', icon='download',
                                         variant='text')
        self._progress = pn.ui.LinearProgress(value=0, variant='determinate', sizing_mode='stretch_width',
                                              visible=False, margin=0)
        self._layout = pn.ui.Column(
            section('Issue list', self._status, pn.ui.Row(self._sync_btn, margin=0), self._result,
                    subtitle='Fetches the open issues with the GitHub CLI. Read-only: nothing is posted to GitHub.'),
            section('Writeups with new activity', self._stale,
                    pn.ui.Row(self._refresh_stale, self._refresh_all, margin=0, sx={'gap': '8px'}), self._progress,
                    subtitle='Issues commented on or edited after they were triaged. Refresh the cached thread, '
                             'then re-triage or review them. Select one to open it.'),
            section('Sync history', self._history),
            sizing_mode='stretch_width', margin=10, sx={'gap': '16px'},
        )
        self._sync_btn.on_click(self._on_sync)
        self._refresh_stale.on_click(lambda e: self._refresh_details([int(n) for n in self._stale.value['issue']]))
        self._refresh_all.on_click(self._on_refresh_all)
        self._stale.param.watch(self._on_select, 'selection')
        self.store.param.watch(self._update, 'version')
        self._update()

    def __panel__(self):
        return self._layout

    @held
    def _update(self, *events):
        cfg = self.store.cfg
        history = github.sync_history(cfg)
        synced = (self.store.synced_at or '')[:16].replace('T', ' ')
        self._status.object = (f'**{len(self.store.open_numbers):,}** open issues in `{cfg.repo}`, last synced '
                               f'{synced} UTC.' if synced else f'`{cfg.repo or "No repository"}` has not been synced yet.')
        self._history.value = pd.DataFrame(
            [{'synced at': h['synced_at'][:16].replace('T', ' '), 'open issues': h['total'],
              'new': len(h['new']), 'closed': len(h['closed'])} for h in reversed(history)],
            columns=['synced at', 'open issues', 'new', 'closed'])
        stale = github.stale(cfg) if cfg.issue_list.exists() else []
        self._stale.value = pd.DataFrame(stale, columns=['issue', 'title', 'category', 'triaged_at', 'updated_at',
                                                         'comments'])
        self._refresh_stale.disabled = not stale

    async def _on_sync(self, event):
        if not self.store.cfg.repo:
            self._notify('Set the repository first (Setup in the top bar).', 'warning')
            return
        self._sync_btn.loading = True
        try:
            summary = await asyncio.to_thread(github.sync, self.store.cfg)
        except Exception as e:
            self._result.objects = [pn.ui.Alert(object=f'Sync failed: {e}', severity='error',
                                                sizing_mode='stretch_width', margin=0)]
            return
        finally:
            self._sync_btn.loading = False
        await asyncio.to_thread(core.build_index, self.store.cfg)
        parts = [f"Fetched **{summary['total']:,}** open issues."]
        if summary['new']:
            parts.append(f"New: {', '.join(f'#{n}' for n in summary['new'][:20])}.")
        if summary['closed']:
            parts.append(f"Closed since last time: {', '.join(f'#{n}' for n in summary['closed'][:20])}.")
        self._result.objects = [pn.ui.Alert(object=' '.join(parts), severity='success', sizing_mode='stretch_width',
                                            margin=0)]
        self.store.refresh(force=True)
        self._notify('Issue list synced', 'success')

    def _on_refresh_all(self, event):
        open_numbers = self.store.open_numbers
        numbers = [int(n) for n in self.store.df['issue'] if int(n) in open_numbers]
        return self._refresh_details(numbers)

    async def _refresh_details(self, numbers: list[int]):
        if not numbers:
            return
        cfg = self.store.cfg
        self._progress.param.update(value=0, visible=True)
        errors = []
        for i, n in enumerate(numbers, 1):
            try:
                await asyncio.to_thread(github.fetch_issue, cfg, n, True)
            except core.TriageError as e:
                errors.append(f'#{n}: {e}')
            self._progress.value = 100 * i / len(numbers)
        self._progress.visible = False
        self._notify(f'Refreshed {len(numbers) - len(errors)} issue(s)' +
                     (f', {len(errors)} failed' if errors else ''), 'warning' if errors else 'success')
        self._update()

    def _on_select(self, event):
        if event.new:
            self._on_open_issue(int(self._stale.value.iloc[event.new[0]]['issue']))
