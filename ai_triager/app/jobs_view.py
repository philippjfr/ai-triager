from __future__ import annotations

import re
import sys

from pathlib import Path

import pandas as pd
import panel as pn
import param

from .. import core, credentials, jobs
from .issues import render_events
from .store import Store
from .theme import held, section

ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def tail(path: Path, lines: int = 300) -> str:
    if not path.exists():
        return ''
    text = ANSI.sub('', path.read_text(errors='replace'))
    return '\n'.join(text.splitlines()[-lines:])


class JobsView(param.Parameterized):
    """Configure and launch batches, follow running jobs, stop them."""

    job_id = param.String(default=None, allow_None=True)

    def __init__(self, store: Store, notify, **params):
        super().__init__(**params)
        self.store = store
        self.notify = notify
        self.mode = pn.ui.RadioButtonGroup(options=['triage', 'review'], value='triage', label='Mode')
        self.runner = pn.ui.Select(label='Runner', width=160)
        self.model = pn.ui.AutocompleteInput(label='Model', restrict=False, min_characters=0,
                                            sizing_mode='stretch_width',
                                            placeholder='provider:model, e.g. anthropic:claude-haiku-4-5')
        self.count = pn.ui.IntInput(label='Issues', value=5, start=1, width=110)
        self.workers = pn.ui.IntInput(label='Parallel workers', value=1, start=1, end=16, width=140)
        self.per_batch = pn.ui.IntInput(label='Issues per session', value=5, start=1, width=150, visible=False)
        self.order = pn.ui.Select(label='Order', options=['oldest', 'newest', 'stale', 'random'], value='oldest',
                                 width=130)
        self.label = pn.ui.TextInput(label='Label contains', width=170)
        self.issues = pn.ui.TextInput(label='Specific issues', placeholder='e.g. 1234 1240', sizing_mode='stretch_width')
        self.effort = pn.ui.Select(label='Thinking effort', options=['default', 'low', 'medium', 'high', 'xhigh'],
                                  value='default', width=150)
        self.steps = pn.ui.IntInput(label='Max model requests per issue', value=150, start=5, width=230)
        self.key_status = pn.ui.Column(margin=0, sizing_mode='stretch_width')
        self.preview_md = pn.ui.Markdown(sizing_mode='stretch_width')
        preview = pn.ui.Button(label='Preview', icon='visibility', variant='outlined')
        launch = pn.ui.Button(label='Launch', icon='rocket_launch', color='primary')
        preview.on_click(self._preview)
        launch.on_click(self._launch)
        for w in (self.mode, self.runner, self.model):
            w.param.watch(self._sync_form, 'value')
        self.model.param.watch(self._sync_form, 'value_input')

        self.jobs_table = pn.ui.Tabulator(
            show_index=False, disabled=True, selectable=1, sizing_mode='stretch_width', height=260, theme='materialize',
            layout='fit_columns', widths={'id': 200, 'model': 230},
            formatters={'cost': {'type': 'money', 'symbol': '$', 'precision': 3}},
        )
        self.jobs_table.param.watch(self._on_select_job, 'selection')
        self.job_header = pn.ui.Typography('', variant='h6', margin=0)
        self.stop_btn = pn.ui.Button(label='Stop', icon='stop', color='error', variant='outlined', visible=False)
        self.stop_btn.on_click(self._stop)
        self.processed = pn.ui.Tabulator(show_index=False, disabled=True, sizing_mode='stretch_width', theme='materialize',
                                              height=240, layout='fit_columns')
        self.log = pn.ui.CodeEditor(language='text', readonly=True, sizing_mode='stretch_width', height=320)
        self.live = pn.ui.Markdown(sizing_mode='stretch_width', disable_anchors=True)

        form = section(
            'Launch a batch',
            pn.ui.Row(self.mode, self.runner, self.model, sizing_mode='stretch_width', margin=0),
            pn.ui.Row(self.count, self.workers, self.per_batch, self.order, self.label, sizing_mode='stretch_width',
                      margin=0),
            pn.ui.Row(self.issues, self.effort, self.steps, sizing_mode='stretch_width', margin=0),
            self.key_status,
            pn.ui.Row(preview, launch, margin=0),
            self.preview_md,
            subtitle='Agents claim issues, triage or review them, and write one writeup per issue to the issues folder.',
        )
        self.detail = detail = section(
            'Job',
            pn.ui.Row(self.job_header, pn.ui.HSpacer(), self.stop_btn, sizing_mode='stretch_width', margin=0),
            pn.ui.Tabs(('Issues', self.processed), ('Log', self.log), ('Live agent', self.live),
                       sizing_mode='stretch_width'),
            visible=False,
        )
        self.panel = pn.ui.Column(
            form, section('Jobs', self.jobs_table, subtitle='Select a job to follow it.'), detail,
            sizing_mode='stretch_width', margin=10, sx={'gap': '16px'},
        )
        self.reset_form()

    @held
    def reset_form(self):
        agents = self.store.cfg.agents
        self.runner.options = ['builtin'] + list(agents.get('runners', {}))
        self.runner.value = agents.get('runner', 'builtin')
        self.count.value = int(agents.get('per_batch', 5))
        self.workers.value = int(agents.get('workers', 1))
        self.order.value = agents.get('order') or 'oldest'
        self.label.value = agents.get('label') or ''
        self._sync_form()
        self.refresh_jobs()

    @held
    def _sync_form(self, *events):
        agents = self.store.cfg.agents
        builtin = self.runner.value == 'builtin'
        self.per_batch.visible = not builtin
        self.effort.visible = builtin
        self.model.options = list(agents.get('models', []))
        if not events or any(e.obj is self.mode or e.obj is self.runner for e in events):
            self.model.value = agents.get(f'{self.mode.value}_model') or ''
        if not events or any(e.obj is self.mode for e in events):
            self.steps.value = int(agents.get('modes', {}).get(self.mode.value, {}).get('steps') or 150)
        self.order.visible = self.label.visible = self.mode.value == 'triage'
        model = self.model.value_input or self.model.value
        if builtin and model:
            env_var = credentials.PROVIDERS.get(credentials.provider_of(model))
            status = credentials.status().get(env_var) if env_var else 'n/a'
            if env_var and not status:
                self.key_status.objects = [pn.ui.Alert(
                    object=f'No API key for this provider: set `{env_var}` or add it under Settings.',
                    alert_type='warning', sizing_mode='stretch_width')]
                return
        self.key_status.objects = []

    def _spec(self) -> dict:
        issues = [int(i) for i in re.findall(r'\d+', self.issues.value or '')]
        return {
            'mode': self.mode.value, 'runner': self.runner.value,
            'model': (self.model.value_input or self.model.value or '').strip(),
            'count': len(issues) or self.count.value, 'workers': self.workers.value,
            'per_batch': self.per_batch.value, 'order': self.order.value, 'label': self.label.value.strip(),
            'issues': issues, 'effort': None if self.effort.value == 'default' else self.effort.value,
            'steps': self.steps.value,
        }

    def _preview(self, event):
        spec, cfg = self._spec(), self.store.cfg
        if spec['mode'] == 'review':
            picked = core.claim_review(cfg, spec['count'], 'preview', dry_run=True, issues=spec['issues'] or None)
            lines = [f"- #{m['issue']} `{m['category']}` / {m.get('recommendation')}: {m.get('title', '')}"
                     for m in picked]
        else:
            picked = core.claim_next(cfg, spec['count'], 'preview', spec['order'], spec['label'] or None,
                                     spec['issues'] or None, dry_run=True)
            lines = [f"- #{i['number']} {i['title']}" for i in picked]
        self.preview_md.object = (f'**Would process {len(lines)} issue(s):**\n\n' + '\n'.join(lines)
                                  if lines else 'Nothing to process with these settings.')

    def _launch(self, event):
        spec = self._spec()
        if not spec['model']:
            self.notify('Choose a model first.', 'warning')
            return
        job = jobs.spawn(self.store.cfg, spec, python=sys.executable)
        self.notify(f"Started job {job['id']}", 'success')
        self.preview_md.object = ''
        self.refresh_jobs()
        self.jobs_table.selection = [0]

    # -- job list and detail ------------------------------------------------

    def refresh_jobs(self):
        rows = []
        for job in jobs.list_jobs(self.store.cfg):
            totals = job.get('totals') or {}
            rows.append({
                'id': job['id'], 'status': job.get('status'), 'mode': job.get('mode'),
                'runner': job.get('runner', 'builtin'), 'model': job.get('model'),
                'done': totals.get('issues', len(job.get('processed') or [])),
                # External runners don't report spend, so their cost is unknown rather than zero.
                'of': job.get('count'),
                'cost': totals.get('cost') if job.get('runner', 'builtin') == 'builtin' else None,
            })
        df = pd.DataFrame(rows, columns=['id', 'status', 'mode', 'runner', 'model', 'done', 'of', 'cost'])
        selected = self.job_id
        self.jobs_table.value = df
        if selected in list(df['id']):
            self.jobs_table.param.update(selection=[list(df['id']).index(selected)])

    def _on_select_job(self, event):
        if event.new:
            self.job_id = self.jobs_table.value.iloc[event.new[0]]['id']
            self.update_job()

    @held
    def update_job(self):
        if not self.job_id:
            return
        cfg = self.store.cfg
        try:
            job = jobs.read_job(cfg, self.job_id)
        except (OSError, ValueError):
            return
        self.detail.visible = True
        status = job.get('status')
        totals = job.get('totals') or {}
        cost = f", ${totals['cost']:.3f}" if totals.get('cost') else ''
        self.job_header.object = (f"{job['id']}: {status}, {job.get('mode')} with {job.get('model')}"
                                  f" ({totals.get('issues', 0)}/{job.get('count')} issues{cost})")
        self.stop_btn.visible = status in ('running', 'queued')
        rows = []
        for p in job.get('processed') or []:
            rows.append({k: p.get(k) for k in ('issue', 'batch', 'worker', 'outcome', 'category', 'confidence',
                                               'recommendation', 'status', 'duration', 'cost', 'error', 'exit_code')
                         if k in p})
        self.processed.value = pd.DataFrame(rows)
        self.log.value = tail(cfg.jobs / f"{job['id']}.log")
        current = job.get('current') or {}
        parts = []
        for worker, issue in sorted(current.items()):
            events = cfg.logs / job['id'] / f'{issue}.jsonl'
            if events.exists():
                rendered = render_events(events).split('\n\n')
                parts.append(f'### Worker {worker}: #{issue}\n\n' + '\n\n'.join(rendered[-12:]))
        self.live.object = '\n\n'.join(parts) or ('No agent session running.' if job.get('runner', 'builtin') == 'builtin'
                                                  else 'External runners log per batch; see the Log tab.')

    def _stop(self, event):
        jobs.stop(self.store.cfg, self.job_id)
        self.notify(f'Stopping {self.job_id}', 'info')

    def tick(self):
        """Called periodically while the view is visible."""
        self.refresh_jobs()
        self.update_job()
