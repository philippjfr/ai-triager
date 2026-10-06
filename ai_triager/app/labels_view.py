"""Label review: launch a decision model over issues, then accept or dismiss its suggestions."""
from __future__ import annotations

import asyncio
import html
import json
import re
import sys

import pandas as pd
import panel as pn
import param

from .. import core, decisions, jobs, labels
from .store import Store
from .theme import held, resizable

STATUS_LABELS = {'pending': 'needs changes', 'ok': 'labels fine', 'applied': 'applied', 'dismissed': 'dismissed',
                 'error': 'failed'}


class LabelsView(pn.viewable.Viewer):

    store = param.ClassSelector(class_=Store)

    def __init__(self, notify, **params):
        super().__init__(**params)
        self._notify = notify
        self._data: dict[int, dict] = {}
        self._current: int | None = None
        self._toggles: dict[str, tuple[str, pn.ui.Switch]] = {}
        self._stamp = None

        # A flex basis instead of stretch_width, so a narrow window wraps the controls below the stats.
        self._stats = pn.ui.Typography('', variant='body2', margin=0, sx={'flex': '1 1 300px'})
        self._scope = pn.ui.Select(label='Which issues', value='unreviewed', width=180, size='small', margin=0,
                                   options={'Not reviewed yet': 'unreviewed', 'Without labels': 'unlabelled',
                                            'Triaged, not reviewed': 'triaged', 'All open issues': 'all'})
        self._count = pn.ui.IntInput(label='How many', value=25, start=1, width=110, size='small', margin=0)
        self._model = pn.ui.Select(label='Model', width=220, size='small', margin=0)
        self._workers = pn.ui.IntInput(label='In parallel', value=4, start=1, end=16, width=100, size='small',
                                       margin=0)
        self._sync = pn.ui.IconButton(icon='sync', description='Sync the label catalogue from GitHub', margin=0)
        self._start = pn.ui.Button(label='Review labels', icon='label', color='primary', margin=0,
                                   description='A decision model such as Jev estimates how likely each catalogue '
                                               'label applies; nothing changes on GitHub until you apply it')

        self._show = pn.ui.Select(label='Show', value='pending', width=170, size='small', margin=0, options={
            'Needs changes': 'pending', 'All reviewed': 'all', 'Labels fine': 'ok', 'Applied': 'applied',
            'Dismissed': 'dismissed'})
        self._count_label = pn.ui.Typography('', variant='caption', margin=0, sx={'color': 'text.secondary'})
        self._table = pn.ui.Tabulator(
            show_index=False, disabled=True, selectable=1, pagination=None, theme='materialize', margin=0,
            sizing_mode='stretch_both', min_height=300, layout='fit_columns',
            widths={'issue': 64, 'change': 210, 'confidence': 80, 'status': 110},
            titles={'confidence': 'conf', 'change': 'suggested change'},
            formatters={'issue': {'type': 'plaintext'}, 'title': {'type': 'textarea'},
                        'change': {'type': 'textarea'}},
        )
        self._heading = pn.ui.Typography('Select an issue', variant='subtitle1', margin=0,
                                         sizing_mode='stretch_width')
        self._link = pn.ui.IconButton(icon='open_in_new', target='_blank', visible=False,
                                      description='Open on GitHub', margin=0)
        self._meta = pn.ui.Typography('', variant='caption', margin=0, sx={'color': 'text.secondary'})
        self._excerpt = pn.ui.Typography('', variant='body2', margin=0, sx={
            'color': 'text.secondary', 'display': '-webkit-box', 'WebkitLineClamp': 4,
            'WebkitBoxOrient': 'vertical', 'overflow': 'hidden'})
        self._changes_box = pn.ui.Column(sizing_mode='stretch_width', margin=0, sx={'gap': '2px'})
        self._keep_box = pn.ui.Column(sizing_mode='stretch_width', margin=0, sx={'gap': '2px'})
        self._apply = pn.ui.Button(label='Apply on GitHub…', icon='send', color='primary', size='small', margin=0)
        self._dismiss = pn.ui.Button(label='Dismiss', icon='close', variant='text', size='small', margin=0)
        self._bulk = pn.ui.Button(label='Apply all high-confidence…', icon='done_all', variant='text', size='small',
                                  margin=0)

        self._confirm_list = pn.ui.Markdown(sizing_mode='stretch_width', margin=0)
        self._confirm_go = pn.ui.Button(label='Change labels on GitHub', icon='send', color='primary')
        self._confirm_result = pn.ui.Column(sizing_mode='stretch_width', margin=0)
        self._confirm = pn.ui.Dialog(
            pn.ui.Column(pn.ui.Typography('These label and issue type changes will be made on GitHub as you.', variant='body2',
                                          margin=0),
                         self._confirm_list, self._confirm_go, self._confirm_result,
                         sizing_mode='stretch_width', margin=0, sx={'gap': '12px'}),
            title='Change labels', open=False, width_option='sm', show_close_button=True,
        )
        self._pending_apply: list[tuple[int, list[str], list[str], str | None]] = []

        def overline(text):
            return pn.ui.Typography(text, variant='overline', margin=(6, 0, 0, 0), sx={'color': 'text.secondary'})

        detail = pn.ui.Paper(
            pn.ui.Row(self._heading, self._link, align='start', sizing_mode='stretch_width', margin=0),
            self._meta, self._excerpt,
            pn.ui.Column(
                overline('Suggested changes'), self._changes_box, overline('Labels to keep'), self._keep_box,
                sizing_mode='stretch_both', scroll=True, margin=0, sx={'flex': 1, 'minHeight': 0},
            ),
            pn.ui.Row(self._apply, self._dismiss, margin=0, sizing_mode='stretch_width',
                      sx={'gap': '8px', 'pt': 1, 'borderTop': 1, 'borderColor': 'divider',
                          'justifyContent': 'flex-start'}),
            variant='outlined', sizing_mode='stretch_both', margin=0,
            sx={'p': 2, 'display': 'flex', 'flexDirection': 'column', 'gap': '6px', 'minHeight': 0},
        )
        toolbar = pn.ui.Paper(
            pn.ui.Row(self._stats,
                      pn.ui.Row(self._scope, self._count, self._model, self._workers, self._start, self._sync,
                                align='center', margin=0, sx={'gap': '12px', 'alignItems': 'center'}),
                      align='center', margin=0, sizing_mode='stretch_width',
                      sx={'gap': '12px', 'flexWrap': 'wrap', 'alignItems': 'center'}),
            variant='outlined', margin=0, sizing_mode='stretch_width', sx={'px': 2, 'py': 1.5},
        )
        self._layout = pn.ui.Column(
            toolbar,
            resizable(
                pn.ui.Column(pn.ui.Row(self._show, self._count_label, pn.ui.HSpacer(), self._bulk, align='center',
                                       margin=0, sizing_mode='stretch_width', sx={'gap': '12px'}),
                             self._table, sizing_mode='stretch_both', margin=0, sx={'gap': '8px'}),
                detail, sizes=(55, 45), min_size=(420, 400), height='max(420px, calc(100vh - 200px))',
            ),
            self._confirm,
            sizing_mode='stretch_width', margin=10, sx={'gap': '12px'},
        )
        self._sync.on_click(self._on_sync)
        self._start.on_click(self._on_start)
        self._show.param.watch(lambda e: self._filter(), 'value')
        self._table.param.watch(self._on_select, 'selection')
        self._apply.on_click(self._on_apply_one)
        self._dismiss.on_click(self._on_dismiss)
        self._bulk.on_click(self._on_bulk)
        self._confirm_go.on_click(self._on_confirm)
        self.reload()

    def __panel__(self):
        return self._layout

    # -- data ---------------------------------------------------------------

    @held
    def reload(self, *events):
        cfg = self.store.cfg
        self._data = labels.results(cfg)
        catalogue = labels.catalogue(cfg)
        statuses = [r.get('status') for r in self._data.values()]
        try:
            synced = json.loads(labels.catalogue_path(cfg).read_text())['synced_at'][:10]
        except (OSError, ValueError, KeyError):
            synced = 'never synced'
        self._stats.object = (
            f"**{statuses.count('pending'):,}** need changes · **{statuses.count('ok'):,}** fine · "
            f"**{statuses.count('applied'):,}** applied · {len(self._data):,}/{len(self.store.open_numbers):,} "
            f"reviewed · {len(catalogue)} labels")
        self._sync.description = f'Sync the label catalogue from GitHub (last synced {synced})'
        agents = cfg.agents
        # Decision models give calibrated per-label probabilities; LLMs fall back to structured output.
        decision = [f"{p}:{spec['default_model']}" for p, spec in decisions.PROVIDERS.items()]
        configured = agents.get('decision_model') or ''
        if configured and decisions.is_decision_model(configured) and configured not in decision:
            decision.insert(0, configured)
        llms = [m for m in dict.fromkeys(agents.get('models', []) + [agents.get('triage_model') or ''])
                if m and not decisions.is_decision_model(m)]
        if configured and not decisions.is_decision_model(configured) and configured not in llms:
            llms.insert(0, configured)
        self._model.groups = {'Decision models': decision, 'LLMs (structured output)': llms}
        if not self._model.value:
            self._model.value = configured or decision[0]
        self._filter()

    def poll(self):
        """Pick up results written by a running labels job."""
        path = labels.results_path(self.store.cfg)
        stamp = path.stat().st_mtime if path.exists() else None
        if stamp != self._stamp:
            self._stamp = stamp
            self.reload()

    def _filter(self):
        self._listing = {i['number']: i for i in core.load_issue_list(self.store.cfg, required=False)['issues']}
        show = self._show.value
        rows = []
        for n, r in sorted(self._data.items()):
            if show != 'all' and r.get('status') != show:
                continue
            kind = r.get('type') or {}
            type_change = [f"type → {kind['suggested']}"] if kind.get('change') else []
            change = ' · '.join(type_change + [f"+{a['label']}" for a in r.get('add', [])]
                                + [f'−{x}' for x in r.get('remove', [])])
            rows.append({'issue': n, 'title': self._listing.get(n, {}).get('title', ''),
                         'change': change if r.get('status') == 'pending' else '',
                         'confidence': r.get('confidence', ''),
                         'status': STATUS_LABELS.get(r.get('status'), r.get('status'))})
        columns = ['issue', 'title', 'change', 'confidence'] + (['status'] if show == 'all' else [])
        self._table.value = pd.DataFrame(rows, columns=['issue', 'title', 'change', 'confidence', 'status'])[columns]
        self._count_label.object = f'{len(rows)} issue(s)'
        if rows and self._current not in {r['issue'] for r in rows}:
            self._table.selection = [0]
        elif rows and self._current is not None:
            self._show_issue(self._current)

    def _on_select(self, event):
        if event.new:
            self._show_issue(int(self._table.value.iloc[event.new[0]]['issue']))

    @held
    def _show_issue(self, n: int):
        self._current = n
        r = self._data.get(n, {})
        issue = getattr(self, '_listing', {}).get(n, {})
        self._heading.object = f"#{n} {issue.get('title', '')}"
        self._link.param.update(href=f'https://github.com/{self.store.cfg.repo}/issues/{n}', visible=True)
        self._meta.object = (f"Type: **{issue.get('type') or 'none'}** · judged by `{r.get('model', '?')}` with "
                             f"{r.get('confidence', '?')} confidence" + (f" on {r['at'][:10]}" if r.get('at') else ''))
        # Issue bodies contain HTML and markdown images; show a plain-text excerpt.
        body = re.sub(r'<[^>]+>', ' ', issue.get('body') or '')
        body = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', body)            # images, e.g. screenshots
        body = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', body)          # links keep their text
        body = ' '.join(body.replace('`', '').split())
        self._excerpt.object = html.escape(body[:600]) or 'No description.'
        self._toggles = {}
        changes, keep = [], []
        kind = r.get('type') or {}
        if kind.get('change'):
            switch = pn.ui.Switch(label=f"set type {kind['suggested']}", value=True, margin=0)
            self._toggles['__type__'] = ('type', switch, kind['suggested'])
            changes.append(self._label_row(switch, kind.get('p'), f"replaces {kind.get('current') or 'no type'}"))
        elif kind.get('suggested'):
            keep.append(self._label_row(pn.ui.Typography(f"type: {kind['suggested']}", variant='body2', margin=0),
                                        kind.get('p'), 'current type fits'))
        for a in r.get('add', []):
            switch = pn.ui.Switch(label=f"add {a['label']}", value=True, margin=0)
            self._toggles[a['label']] = ('add', switch, None)
            changes.append(self._label_row(switch, a.get('p'), a['reason']))
        for c in r.get('current', []):
            if c['keep']:
                keep.append(self._label_row(pn.ui.Typography(c['label'], variant='body2', margin=0), c.get('p'),
                                            c['reason']))
                continue
            switch = pn.ui.Switch(label=f"remove {c['label']}", value=True, margin=0)
            self._toggles[c['label']] = ('remove', switch, None)
            changes.append(self._label_row(switch, c.get('p'), c['reason']))
        none = pn.ui.Typography('None', variant='body2', margin=0, sx={'color': 'text.secondary'})
        self._changes_box.objects = changes or [none]
        self._keep_box.objects = keep or [pn.ui.Typography('None', variant='body2', margin=0,
                                                           sx={'color': 'text.secondary'})]
        editable = r.get('status') == 'pending'
        self._apply.disabled = self._dismiss.disabled = not editable

    @staticmethod
    def _label_row(control, p: float | None, reason: str) -> pn.ui.Row:
        """A label with its probability as a small bar, or the model's reason when there is no probability."""
        if p is None:
            detail = [pn.ui.Typography(reason, variant='caption', margin=0, sizing_mode='stretch_width',
                                       sx={'color': 'text.secondary'})]
        else:
            detail = [
                pn.ui.LinearProgress(value=round(100 * p), variant='determinate', width=120, margin=0,
                                     align='center', sx={'height': 6, 'borderRadius': 3}),
                pn.ui.Typography(f'{p:.0%}', variant='data', width=44, margin=0, align='center',
                                 sx={'textAlign': 'right'}),
            ]
        return pn.ui.Row(pn.ui.Column(control, width=260, margin=0), *detail, align='center', margin=0,
                         sizing_mode='stretch_width', sx={'gap': '8px', 'minHeight': '32px'})

    # -- actions ------------------------------------------------------------

    async def _on_sync(self, event):
        self._sync.loading = True
        try:
            found = await asyncio.to_thread(labels.sync_catalogue, self.store.cfg)
            self._notify(f'Synced {len(found)} labels', 'success')
        except Exception as e:
            self._notify(f'Label sync failed: {e}', 'error')
        finally:
            self._sync.loading = False
        self.reload()

    def _on_start(self, event):
        model = (self._model.value or '').strip()
        if not model:
            self._notify('Choose a decision model first.', 'warning')
            return
        cfg = self.store.cfg
        job = jobs.spawn(cfg, {'mode': 'labels', 'model': model, 'scope': self._scope.value,
                               'count': self._count.value, 'workers': self._workers.value, 'runner': 'builtin'},
                         python=sys.executable)
        self._notify(f"Started label review of up to {self._count.value} issues (job {job['id']})", 'success')

    def _changes_for_current(self) -> tuple[list[str], list[str], str | None]:
        add = [label for label, (kind, s, _) in self._toggles.items() if kind == 'add' and s.value]
        remove = [label for label, (kind, s, _) in self._toggles.items() if kind == 'remove' and s.value]
        kind = self._toggles.get('__type__')
        return add, remove, (kind[2] if kind and kind[1].value else None)

    def _open_confirm(self, items: list[tuple[int, list[str], list[str], str | None]]):
        self._pending_apply = [i for i in items if i[1] or i[2] or i[3]]
        if not self._pending_apply:
            self._notify('Nothing to change.', 'info')
            return
        lines = []
        for n, add, remove, kind in self._pending_apply:
            parts = [f'set type `{kind}`' if kind else '',
                     f'add {", ".join(f"`{a}`" for a in add)}' if add else '',
                     f'remove {", ".join(f"`{r}`" for r in remove)}' if remove else '']
            lines.append(f"- **#{n}**: {'; '.join(p for p in parts if p)}")
        with pn.io.hold():
            self._confirm_list.object = '\n'.join(lines)
            self._confirm_result.objects = []
            self._confirm_go.disabled = False
            self._confirm.open = True

    def _on_apply_one(self, event):
        if self._current is not None:
            add, remove, kind = self._changes_for_current()
            self._open_confirm([(self._current, add, remove, kind)])

    def _on_bulk(self, event):
        items = [(n, [a['label'] for a in r.get('add', [])], r.get('remove', []),
                  (r.get('type') or {}).get('suggested') if (r.get('type') or {}).get('change') else None)
                 for n, r in self._data.items() if r.get('status') == 'pending' and r.get('confidence') == 'high']
        self._open_confirm(items)

    async def _on_confirm(self, event):
        self._confirm_go.disabled = True
        results = []
        for n, add, remove, kind in self._pending_apply:
            results.append(await asyncio.to_thread(labels.apply, self.store.cfg, n, add, remove, kind))
        failed = [r for r in results if not r['ok']]
        msg = f'Updated labels on {len(results) - len(failed)} of {len(results)} issue(s).'
        if failed:
            msg += ' Failed: ' + '; '.join(f"#{r['issue']} ({r['output'][:100]})" for r in failed)
        self._confirm_result.objects = [pn.ui.Alert(object=msg, severity='warning' if failed else 'success',
                                                    sizing_mode='stretch_width', margin=0)]
        self.reload()

    def _on_dismiss(self, event):
        if self._current is not None:
            labels.update_result(self.store.cfg, self._current, status='dismissed')
            self.reload()
