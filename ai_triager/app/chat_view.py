"""Chat with the triage data through Lumen.

Lumen gets the workspace as in-memory DuckDB tables (issues, writeups, label reviews, labels, jobs,
job results, sync history) so it can answer with SQL and charts, plus tools that read details and
take local actions: launching batches, recording close decisions, reviewing labels. Nothing in the
chat writes to GitHub; closing issues and applying labels stay behind the confirmation dialogs on
their pages.
"""
from __future__ import annotations

import functools
import inspect
import sys

import pandas as pd
import panel as pn
import param

from .. import closing, core, credentials, github, jobs, labels
from .store import Store

# Lumen's Anthropic client still passes `temperature`, which anthropic>=1.0 (needed by pydantic-ai) rejects,
# so Anthropic models are left out of the chat until Lumen catches up.
LUMEN_LLMS = {'openai': 'OpenAI', 'google-gla': 'Google', 'openrouter': 'OpenRouter', 'mistral': 'MistralAI',
              'groq': 'Groq'}
EXTENSIONS = ('vega', 'filedropper', 'jsoneditor', 'texteditor', 'tabulator', 'codeeditor')
SUGGESTIONS = [
    ('bar_chart', 'Chart the writeups by category and confidence'),
    ('task_alt', 'Which close recommendations are verified and high confidence?'),
    ('label', 'Which label suggestions should I look at first?'),
    ('schedule', 'How has triage progressed per day and per model?'),
]


def _joined(value) -> str:
    return ', '.join(map(str, value)) if isinstance(value, list) else (value or '')


def build_tables(store: Store) -> dict[str, pd.DataFrame]:
    cfg = store.cfg
    listing = core.load_issue_list(cfg, required=False)['issues']
    issues = pd.DataFrame([{
        'number': i['number'], 'title': i['title'], 'type': i.get('type'), 'author': i.get('user'),
        'labels': _joined(i['labels']),
        'comments': i.get('comments', 0), 'reactions': i.get('reactions', 0),
        'created_at': pd.to_datetime(i['created_at']), 'updated_at': pd.to_datetime(i['updated_at']),
    } for i in listing])
    open_numbers = {i['number'] for i in listing}
    rows = []
    for _, meta, _ in core.load_writeups(cfg):
        if not meta:
            continue
        rows.append({
            'issue': meta.get('issue'), 'title': meta.get('title'), 'category': meta.get('category'),
            'confidence': meta.get('confidence'), 'reproduced': meta.get('reproduced'),
            'recommendation': meta.get('recommendation'), 'summary': meta.get('summary'),
            'verified': meta.get('verified') or 'no', 'verified_by': meta.get('verified_by'),
            'triaged_by': meta.get('triaged_by'), 'model': str(meta.get('triaged_by') or '').split('@')[0],
            'triaged_at': pd.to_datetime(str(meta.get('triaged_at') or ''), errors='coerce'),
            'duplicate_of': meta.get('duplicate_of'), 'fixed_by': _joined(meta.get('fixed_by')),
            'components': _joined(meta.get('components')), 'decision': meta.get('decision'),
            'issue_open': meta.get('issue') in open_numbers,
            **{f.name: _joined(meta.get(f.name)) if f.type == 'list' else meta.get(f.name)
               for f in cfg.extra_fields},
        })
    writeups = pd.DataFrame(rows)
    reviews = pd.DataFrame([{
        'issue': n, 'status': r.get('status'), 'confidence': r.get('confidence'), 'model': r.get('model'),
        'labels_now': _joined(r.get('labels')), 'suggest_add': _joined([a['label'] for a in r.get('add', [])]),
        'suggest_remove': _joined(r.get('remove')),
        'suggest_type': (r.get('type') or {}).get('suggested') if (r.get('type') or {}).get('change') else None,
        'reviewed_at': pd.to_datetime(r.get('at'), errors='coerce'),
    } for n, r in labels.results(cfg).items()])
    job_rows, results = [], []
    for job in jobs.list_jobs(cfg):
        totals = job.get('totals') or {}
        job_rows.append({'job': job['id'], 'mode': job.get('mode'), 'runner': job.get('runner', 'builtin'),
                         'model': job.get('model'), 'status': job.get('status'), 'issues': totals.get('issues'),
                         'cost': totals.get('cost'), 'started': pd.to_datetime(job.get('started'), errors='coerce')})
        for p in job.get('processed') or []:
            if p.get('issue'):
                results.append({'job': job['id'], 'issue': p['issue'], 'outcome': p.get('outcome') or p.get('status'),
                                'category': p.get('category'), 'cost': p.get('cost'),
                                'duration_s': p.get('duration')})
    tables = {
        'issues': issues, 'writeups': writeups, 'label_reviews': reviews,
        'labels': pd.DataFrame(labels.catalogue(cfg), columns=['name', 'description']),
        'jobs': pd.DataFrame(job_rows), 'job_results': pd.DataFrame(results),
        'sync_history': pd.DataFrame([{'synced_at': pd.to_datetime(h['synced_at']), 'open_issues': h['total'],
                                       'new': len(h['new']), 'closed': len(h['closed'])}
                                      for h in github.sync_history(cfg)]),
    }
    for df in tables.values():
        # DuckDB's column stats reject mixed timezone-aware timestamps; store everything as naive UTC.
        for col in df.columns:
            if isinstance(df[col].dtype, pd.DatetimeTZDtype):
                df[col] = df[col].dt.tz_convert('UTC').dt.tz_localize(None)
            elif df[col].dtype == object and df[col].map(lambda v: isinstance(v, pd.Timestamp)).any():
                df[col] = pd.to_datetime(df[col], utc=True, errors='coerce').dt.tz_localize(None)
    return {name: df for name, df in tables.items() if not df.empty}


def _ignore_extra_kwargs(function):
    """Lumen passes plan bookkeeping such as `step_title` to function tools; drop what they don't declare."""
    accepted = set(inspect.signature(function).parameters)

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return function(*args, **{k: v for k, v in kwargs.items() if k in accepted})
    return wrapper


def make_llm(model: str):
    from lumen.ai import llm as lumen_llm

    provider, _, name = model.partition(':')
    if provider not in LUMEN_LLMS:
        raise ValueError(f'{model}: chat supports {", ".join(LUMEN_LLMS)} models')
    return getattr(lumen_llm, LUMEN_LLMS[provider])(model_kwargs={'default': {'model': name}})


class ChatView(pn.viewable.Viewer):

    store = param.ClassSelector(class_=Store)

    def __init__(self, notify, **params):
        super().__init__(**params)
        self._notify = notify
        self._source = None
        self._ui = None
        agents = self.store.cfg.agents
        candidates = [agents.get('chat_model') or '', *agents.get('models', []), agents.get('review_model') or '',
                      agents.get('triage_model') or '']
        llms = [m for m in dict.fromkeys(candidates) if m.partition(':')[0] in LUMEN_LLMS]
        default = llms[0] if llms else ''
        self._model = pn.ui.Select(label='Chat model', options=list(dict.fromkeys([default, *llms])), value=default,
                                   width=280, size='small', margin=0)
        self._reload = pn.ui.Button(label='Reload data', icon='refresh', variant='outlined', size='small', margin=0,
                                    description='Re-read writeups, label reviews and jobs into the chat tables')
        self._status = pn.ui.Typography('', variant='caption', margin=0, sizing_mode='stretch_width',
                                        sx={'color': 'text.secondary', 'alignSelf': 'center'})
        self._holder = pn.ui.Column(sizing_mode='stretch_both', margin=0)
        self._layout = pn.ui.Column(
            pn.ui.Row(self._status, self._model, self._reload, align='center', margin=(0, 10),
                      sizing_mode='stretch_width', sx={'gap': '12px', 'alignItems': 'center', 'pt': 1}),
            self._holder,
            # Flush with the app sidebar so Lumen's navigation drawer starts at the edge.
            sizing_mode='stretch_width', margin=0, sx={'gap': '8px', 'height': 'calc(100vh - 72px)'},
        )
        self._model.param.watch(lambda e: self._build(), 'value')
        self._reload.on_click(lambda e: self._reload_data())

    def __panel__(self):
        return self._layout

    def activate(self):
        """Build the Lumen UI the first time the page is shown."""
        if self._ui is None:
            self._build()

    # -- Lumen setup ---------------------------------------------------------

    def _build(self):
        import lumen.ai as lmai

        from lumen.ai.agents import ChatAgent, SQLAgent, TableListAgent, ValidationAgent, VegaLiteAgent
        from lumen.sources.duckdb import DuckDBSource

        cfg = self.store.cfg
        credentials.apply_to_environ(cfg)
        self._holder.objects = [pn.ui.LinearProgress(variant='indeterminate', sizing_mode='stretch_width')]
        try:
            tables = build_tables(self.store)
            self._source = DuckDBSource.from_df(tables, name='triage')
            self._ui = lmai.ExplorerUI(
                data=self._source, llm=make_llm(self._model.value), tools=self._tools(),
                default_agents=[TableListAgent, ChatAgent, SQLAgent, VegaLiteAgent, ValidationAgent],
                title=f'{cfg.repo} triage chat', suggestions=SUGGESTIONS,
                context={'repository': cfg.repo, 'about': (
                    'Tables describe GitHub issue triage. `issues` are open issues; `writeups` are agent '
                    'diagnoses (category, confidence, recommendation, verification) keyed by `issue`; '
                    '`label_reviews` are label suggestions; `jobs`/`job_results` are agent runs.')},
            )
        except Exception as e:
            self._holder.objects = [pn.ui.Alert(object=f'Could not start the chat: {type(e).__name__}: {e}',
                                                severity='error', sizing_mode='stretch_width', margin=0)]
            return
        # Panel gives Lumen's stretching navigation Paper an implicit 10px margin, which leaves a gap inside its
        # drawer. A tuple differs from the parameter's current value (0), so the override reaches the browser.
        navigation = getattr(self._ui, '_navigation', None)
        if navigation is not None:
            navigation.margin = (0, 0, 0, 0)
        self._holder.objects = [self._ui]
        self._status.object = (f"Chatting over {', '.join(f'`{t}`' for t in tables)} with "
                               f'`{self._model.value}`. Actions stay local; GitHub writes need the Close review '
                               'and Labels pages.')

    def _reload_data(self):
        if self._source is None:
            return self.activate()
        for name, df in build_tables(self.store).items():
            for col in df.select_dtypes(include=['string']).columns:
                df[col] = df[col].astype(object)
            self._source._ingest_table(name, df)
        self._notify('Chat tables reloaded', 'success')

    # -- tools ---------------------------------------------------------------

    def _tools(self) -> list:
        from lumen.ai.tools import define_tool

        store = self.store
        cfg = store.cfg

        def get_issue_details(issue: int) -> str:
            """
            Fetch the full triage writeup and the GitHub discussion of one issue.

            Parameters
            ----------
            issue : int
                The GitHub issue number

            Returns
            -------
            str
                The writeup markdown followed by the cached GitHub thread
            """
            path = core.writeup_path(cfg, issue)
            writeup = path.read_text() if path.exists() else f'No writeup for #{issue} yet.'
            try:
                thread = github.context(cfg, issue, limit=6000, comment_limit=1500, with_env=False)
            except core.TriageError as e:
                thread = f'GitHub thread unavailable: {e}'
            return f'{writeup}\n\n---\n\n{thread}'[:20000]

        @define_tool(render_output=True, purpose='Show an issue\'s triage writeup to the user')
        def show_writeup(issue: int):
            """
            Display the triage writeup of an issue in the chat.

            Parameters
            ----------
            issue : int
                The GitHub issue number
            """
            path = core.writeup_path(cfg, issue)
            if not path.exists():
                return pn.ui.Alert(object=f'No writeup for #{issue}.', severity='info', margin=0)
            meta, body = core.parse_writeup(path)
            header = (f"**#{issue} {meta.get('title', '')}**  \n`{meta.get('category')}` · "
                      f"{meta.get('confidence')} confidence · recommends `{meta.get('recommendation')}` · "
                      f"verified: {meta.get('verified') or 'no'}")
            return pn.ui.Card(pn.ui.Markdown(f'{header}\n\n{body}', sizing_mode='stretch_width'),
                              title=f'Writeup #{issue}', collapsed=False, sizing_mode='stretch_width')

        def start_batch(mode: str, issues: list[int], model: str = '') -> str:
            """
            Start a background agent job that triages or reviews the given issues.

            Parameters
            ----------
            mode : str
                Either "triage" (first-pass diagnosis) or "review" (verify existing writeups)
            issues : list[int]
                Issue numbers to process
            model : str
                Optional pydantic-ai model string; defaults to the configured triage or review model

            Returns
            -------
            str
                The started job's id
            """
            if mode not in ('triage', 'review'):
                return 'mode must be "triage" or "review"'
            agents = store.cfg.agents
            model = model or agents.get(f'{mode}_model')
            job = jobs.spawn(store.cfg, {'mode': mode, 'runner': agents.get('runner', 'builtin'), 'model': model,
                                         'issues': list(issues), 'count': len(issues),
                                         'workers': min(4, len(issues))}, python=sys.executable)
            return f"Started {mode} job {job['id']} on {len(issues)} issue(s) with {model}; follow it on Batches."

        def record_close_decision(issue: int, decision: str, note: str = '') -> str:
            """
            Record a maintainer decision on an issue recommended for closing. This does not close anything
            on GitHub; accepted issues are closed from the Close review page after confirmation.

            Parameters
            ----------
            issue : int
                The GitHub issue number
            decision : str
                "accept" (close with the proposed comment), "rereview" or "keep"
            note : str
                Optional note, e.g. what a re-review should look at

            Returns
            -------
            str
                Confirmation of what was recorded
            """
            if decision not in closing.DECISIONS:
                return f'decision must be one of {", ".join(closing.DECISIONS)}'
            closing.set_decision(store.cfg, issue, decision, store.reviewer() + ' (via chat)', note)
            store.refresh(force=True)
            return f'Recorded "{decision}" for #{issue}. Act on it from the Close review page.'

        def review_issue_labels(issues: list[int]) -> str:
            """
            Start a label review of the given issues with the configured decision model.

            Parameters
            ----------
            issues : list[int]
                Issue numbers whose labels should be checked

            Returns
            -------
            str
                The started job's id
            """
            agents = store.cfg.agents
            model = agents.get('decision_model') or agents.get('triage_model')
            job = jobs.spawn(store.cfg, {'mode': 'labels', 'runner': 'builtin', 'model': model,
                                         'issues': list(issues), 'count': len(issues), 'workers': 4},
                             python=sys.executable)
            return f"Started label review job {job['id']} with {model}; results appear on the Labels page."

        tools = [get_issue_details, show_writeup, start_batch, record_close_decision, review_issue_labels]
        return [_ignore_extra_kwargs(tool) for tool in tools]
