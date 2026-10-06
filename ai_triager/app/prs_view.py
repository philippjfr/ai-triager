"""PR review: a first pass over open pull requests against the template, the AI policy and a smell test."""
from __future__ import annotations

import asyncio
import html
import re

import pandas as pd
import panel as pn
import param

from .. import decisions, prs
from .store import Store
from .theme import held, resizable

STATUS_LABELS = {'ok': 'looks fine', 'attention': 'needs attention', 'concern': 'concerns', 'error': 'failed'}
DECISIONS = {'ready': 'ready for review', 'changes': 'asked for changes', 'dismissed': 'dismissed'}
SCORE_COLUMNS = ('policy', 'template', 'smell')
CHECK_ICONS = {True: ('check_circle', 'success.main'), False: ('cancel', 'error.main'), None: ('info', 'text.secondary')}


def _score_color(value) -> str:
    if value is None or pd.isna(value):
        return ''
    color = '#2e7d32' if value >= 60 else '#b26a00' if value >= 40 else '#c62828'
    return f'color: {color}; font-weight: 600'


class PRReview(pn.viewable.Viewer):

    store = param.ClassSelector(class_=Store)

    def __init__(self, notify, **params):
        super().__init__(**params)
        self._notify = notify
        self._results: dict[int, dict] = {}
        self._pulls: dict[int, dict] = {}
        self._current: int | None = None
        self._running = False

        self._stats = pn.ui.Typography('', variant='body2', margin=0, sx={'flex': '1 1 300px'})
        self._scope = pn.ui.Select(label='Which PRs', value='external', width=200, size='small', margin=0, options={
            'External, not reviewed': 'external', 'All not reviewed': 'unreviewed', 'All open PRs': 'all'})
        self._model = pn.ui.Select(label='Decision model', width=220, size='small', margin=0)
        self._workers = pn.ui.IntInput(label='In parallel', value=4, start=1, end=12, width=100, size='small',
                                       margin=0)
        self._start = pn.ui.Button(label='Review PRs', icon='rate_review', color='primary', margin=0,
                                   description='Checks each PR against the template and policy and asks the decision '
                                               'model about policy, template and a smell test. Nothing is posted.')
        self._sync = pn.ui.IconButton(icon='sync', description='Fetch open pull requests from GitHub', margin=0)
        self._progress = pn.ui.LinearProgress(variant='determinate', value=0, visible=False,
                                              sizing_mode='stretch_width', margin=0)

        self._show = pn.ui.Select(label='Show', value='open', width=190, size='small', margin=0, options={
            'To look at': 'open', 'Concerns': 'concern', 'Needs attention': 'attention', 'Looks fine': 'ok',
            'Decided': 'decided', 'Not reviewed': 'unreviewed', 'All open PRs': 'all'})
        self._hide_trusted = pn.ui.Switch(label='External only', value=True, margin=0)
        self._count_label = pn.ui.Typography('', variant='caption', margin=0, sx={'color': 'text.secondary'})
        self._table = pn.ui.Tabulator(
            show_index=False, disabled=True, selectable=1, pagination=None, theme='materialize', margin=0,
            sizing_mode='stretch_both', min_height=300, layout='fit_columns',
            widths={'pr': 64, 'author': 130, 'status': 120, 'policy': 70, 'template': 80, 'smell': 70},
            formatters={'pr': {'type': 'plaintext'}, 'title': {'type': 'textarea'},
                        **{k: {'type': 'money', 'symbol': '%', 'symbolAfter': True, 'precision': 0}
                           for k in SCORE_COLUMNS}},
            text_align={k: 'right' for k in SCORE_COLUMNS},
        )

        self._heading = pn.ui.Typography('Select a pull request', variant='subtitle1', margin=0,
                                         sizing_mode='stretch_width')
        self._link = pn.ui.IconButton(icon='open_in_new', target='_blank', visible=False,
                                      description='Open on GitHub', margin=0)
        self._chips = pn.ui.Row(margin=0, sx={'gap': '6px', 'flexWrap': 'wrap', 'alignItems': 'center'})
        self._excerpt = pn.ui.Typography('', variant='body2', margin=0, sx={
            'color': 'text.secondary', 'display': '-webkit-box', 'WebkitLineClamp': 4,
            'WebkitBoxOrient': 'vertical', 'overflow': 'hidden'})
        self._scores = pn.ui.Column(sizing_mode='stretch_width', margin=0, sx={'gap': '2px'})
        self._checks = pn.ui.Column(sizing_mode='stretch_width', margin=0, sx={'gap': '2px'})
        self._files = pn.ui.Typography('', variant='body2', margin=0, sizing_mode='stretch_width')
        self._comment = pn.ui.TextAreaInput(label='Comment for the contributor', rows=6, sizing_mode='stretch_width',
                                            margin=0)
        self._draft = pn.ui.Button(label='Draft comment', icon='edit_note', variant='outlined', size='small', margin=0,
                                   description='An LLM drafts a comment listing what is missing; edit it before posting')
        self._post = pn.ui.Button(label='Post on GitHub…', icon='send', color='primary', size='small', margin=0)
        self._ready = pn.ui.Button(label='Ready for review', icon='thumb_up', variant='text', size='small', margin=0)
        self._dismiss = pn.ui.Button(label='Dismiss', icon='close', variant='text', size='small', margin=0)

        self._confirm_text = pn.ui.Markdown(sizing_mode='stretch_width', margin=0)
        self._confirm_go = pn.ui.Button(label='Post comment', icon='send', color='primary')
        self._confirm_result = pn.ui.Column(sizing_mode='stretch_width', margin=0)
        self._confirm = pn.ui.Dialog(
            pn.ui.Column(pn.ui.Typography('This comment will be posted on GitHub as you.', variant='body2', margin=0),
                         self._confirm_text, self._confirm_go, self._confirm_result,
                         sizing_mode='stretch_width', margin=0, sx={'gap': '12px'}),
            title='Post comment', open=False, width_option='sm', show_close_button=True,
        )

        def overline(text):
            return pn.ui.Typography(text, variant='overline', margin=(6, 0, 0, 0), sx={'color': 'text.secondary'})

        detail = pn.ui.Paper(
            pn.ui.Row(self._heading, self._link, align='start', sizing_mode='stretch_width', margin=0),
            self._chips, self._excerpt,
            pn.ui.Column(
                overline('Decision model'), self._scores, overline('Checks'), self._checks,
                overline('Changed files'), self._files, overline('Comment'), self._comment,
                pn.ui.Row(self._draft, margin=0),
                sizing_mode='stretch_both', scroll=True, margin=0, sx={'flex': 1, 'minHeight': 0, 'gap': '4px'},
            ),
            pn.ui.Row(self._post, self._ready, self._dismiss, margin=0, sizing_mode='stretch_width',
                      sx={'gap': '8px', 'pt': 1, 'borderTop': 1, 'borderColor': 'divider'}),
            variant='outlined', sizing_mode='stretch_both', margin=0,
            sx={'p': 2, 'display': 'flex', 'flexDirection': 'column', 'gap': '6px', 'minHeight': 0},
        )
        toolbar = pn.ui.Paper(
            pn.ui.Row(self._stats,
                      pn.ui.Row(self._scope, self._model, self._workers, self._start, self._sync, margin=0,
                                sx={'gap': '12px', 'alignItems': 'center'}),
                      margin=0, sizing_mode='stretch_width',
                      sx={'gap': '12px', 'flexWrap': 'wrap', 'alignItems': 'center'}),
            self._progress,
            variant='outlined', margin=0, sizing_mode='stretch_width', sx={'px': 2, 'py': 1.5},
        )
        self._layout = pn.ui.Column(
            toolbar,
            resizable(
                pn.ui.Column(pn.ui.Row(self._show, self._hide_trusted, self._count_label, margin=0,
                                       sizing_mode='stretch_width', sx={'gap': '12px', 'alignItems': 'center'}),
                             self._table, sizing_mode='stretch_both', margin=0, sx={'gap': '8px'}),
                detail, sizes=(55, 45), min_size=(440, 420), height='max(460px, calc(100vh - 200px))',
            ),
            self._confirm,
            sizing_mode='stretch_width', margin=10, sx={'gap': '12px'},
        )
        self._sync.on_click(self._on_sync)
        self._start.on_click(self._on_start)
        self._show.param.watch(lambda e: self._filter(), 'value')
        self._hide_trusted.param.watch(lambda e: self._filter(), 'value')
        self._table.param.watch(self._on_select, 'selection')
        self._draft.on_click(self._on_draft)
        self._post.on_click(self._on_post)
        self._confirm_go.on_click(self._on_confirm)
        self._ready.on_click(lambda e: self._decide('ready'))
        self._dismiss.on_click(lambda e: self._decide('dismissed'))
        self.reload()

    def __panel__(self):
        return self._layout

    # -- data ---------------------------------------------------------------

    @held
    def reload(self, *events):
        cfg = self.store.cfg
        listing = prs.load(cfg)
        self._pulls = {p['number']: p for p in listing['pulls']}
        self._results = prs.results(cfg)
        current = [self._results[n] for n in self._pulls if n in self._results]
        stale = sum(r.get('sha') != self._pulls[r['number']].get('sha') for r in current if 'number' in r)
        counts = {s: sum(r.get('status') == s for r in current) for s in STATUS_LABELS}
        synced = (listing.get('synced_at') or 'never')[:10]
        self._stats.object = (
            f"**{len(self._pulls)}** open PRs · **{counts['concern']}** concerns · **{counts['attention']}** need "
            f"attention · **{counts['ok']}** look fine · {stale} changed since review · synced {synced}")
        opts = prs.settings(cfg)
        choices = [f"{p}:{spec['default_model']}" for p, spec in decisions.PROVIDERS.items()]
        if decisions.is_decision_model(opts['model']) and opts['model'] not in choices:
            choices.insert(0, opts['model'])
        self._model.options = choices
        if self._model.value not in choices:
            self._model.value = opts['model'] if opts['model'] in choices else choices[0]
        self._filter()

    def _row_status(self, n: int) -> str:
        r = self._results.get(n)
        if not r:
            return 'unreviewed'
        if r.get('decision'):
            return 'decided'
        return r.get('status', 'unreviewed')

    def _filter(self):
        trusted = set(prs.settings(self.store.cfg)['trusted'])
        show = self._show.value
        rows = []
        for n, pr in sorted(self._pulls.items(), reverse=True):
            if prs.is_bot(pr) or (self._hide_trusted.value and pr.get('association') in trusted):
                continue
            status = self._row_status(n)
            if show == 'open' and status not in ('concern', 'attention'):
                continue
            if show not in ('open', 'all') and status != show:
                continue
            r = self._results.get(n, {})
            scores = r.get('scores') or {}
            label = DECISIONS.get(r.get('decision')) or STATUS_LABELS.get(r.get('status'), 'not reviewed')
            if r and r.get('sha') != pr.get('sha'):
                label += ' (changed)'
            rows.append({'pr': n, 'title': pr['title'], 'author': pr['user'], 'status': label,
                         **{k: round(100 * scores[k]) if k in scores else None
                            for k in ('policy', 'template', 'smell')}})
        columns = ['pr', 'title', 'author', 'status', 'policy', 'template', 'smell']
        self._table.value = pd.DataFrame(rows, columns=columns)
        self._table.style.map(_score_color, subset=list(SCORE_COLUMNS))
        self._count_label.object = f'{len(rows)} PR(s)'
        if rows and self._current not in {r['pr'] for r in rows}:
            self._table.selection = [0]
        elif rows and self._current is not None:
            self._show_pr(self._current)
        elif not rows:
            self._current = None

    def _on_select(self, event):
        if event.new:
            self._show_pr(int(self._table.value.iloc[event.new[0]]['pr']))

    @held
    def _show_pr(self, n: int):
        self._current = n
        pr = self._pulls.get(n, {})
        r = self._results.get(n, {})
        self._heading.object = f"#{n} {pr.get('title', '')}"
        self._link.param.update(href=pr.get('url') or f'https://github.com/{self.store.cfg.repo}/pull/{n}',
                                visible=True)
        chips = [(f"@{pr.get('user', '?')}", 'default'), ((pr.get('association') or '').lower().replace('_', ' '),
                                                           'default')]
        if r.get('first_time'):
            chips.append(('first contribution', 'info'))
        if pr.get('draft'):
            chips.append(('draft', 'default'))
        if r.get('open_prs_in_org') is not None:
            chips.append((f"{r['open_prs_in_org']} open PRs in org", 'warning'
                          if r['open_prs_in_org'] > prs.settings(self.store.cfg)['max_open'] else 'default'))
        if r.get('status'):
            chips.append((DECISIONS.get(r.get('decision')) or STATUS_LABELS.get(r['status'], r['status']),
                          {'ok': 'success', 'attention': 'warning', 'concern': 'error'}.get(r['status'], 'default')))
        if r and r.get('sha') != pr.get('sha'):
            chips.append(('changed since review', 'warning'))
        self._chips.objects = [pn.ui.Chip(object=text, color=color, size='small', variant='outlined', margin=0)
                               for text, color in chips if text]
        body = re.sub(r'<!--.*?-->', ' ', pr.get('body') or '', flags=re.S)
        body = re.sub(r'<[^>]+>|!\[[^\]]*\]\([^)]*\)', ' ', body)
        body = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', body)
        self._excerpt.object = html.escape(' '.join(body.replace('`', '').split())[:600]) or 'No description.'
        if r.get('status') == 'error':
            self._scores.objects = [pn.ui.Alert(object=r.get('error', 'failed'), severity='error', margin=0,
                                                sizing_mode='stretch_width')]
        elif not r:
            self._scores.objects = [pn.ui.Typography('Not reviewed yet.', variant='body2', margin=0,
                                                     sx={'color': 'text.secondary'})]
        else:
            self._scores.objects = [self._score_row(k, p) for k, p in (r.get('scores') or {}).items()]
        self._checks.objects = [self._check_row(c) for c in r.get('checks', [])] or [
            pn.ui.Typography('None yet.', variant='body2', margin=0, sx={'color': 'text.secondary'})]
        files = r.get('files') or []
        self._files.object = html.escape(', '.join(
            f"{f['path']} (+{f['additions']} −{f['deletions']})" for f in files[:20])
            + (f' and {len(files) - 20} more' if len(files) > 20 else '')) or '—'
        self._comment.value = r.get('comment') or ''
        posted = r.get('posted')
        self._post.disabled = not r or bool(posted and posted >= r.get('at', ''))
        self._post.label = 'Posted' if self._post.disabled and posted else 'Post on GitHub…'
        self._draft.disabled = self._ready.disabled = self._dismiss.disabled = not r or r.get('status') == 'error'

    @staticmethod
    def _score_row(key: str, p: float) -> pn.ui.Row:
        good = 1 - p if key in prs.NEGATIVE else p
        color = 'success' if good >= 0.6 else 'warning' if good >= 0.4 else 'error'
        return pn.ui.Row(
            pn.ui.Typography(prs.QUESTION_LABELS.get(key, key), variant='body2', width=230, margin=0, align='center'),
            pn.ui.LinearProgress(value=round(100 * p), variant='determinate', color=color, width=140, margin=0,
                                 align='center', sx={'height': 6, 'borderRadius': 3}),
            pn.ui.Typography(f'{p:.0%}', variant='data', width=44, margin=0, align='center', sx={'textAlign': 'right'}),
            margin=0, sizing_mode='stretch_width', sx={'gap': '8px', 'minHeight': '28px', 'alignItems': 'center'})

    @staticmethod
    def _check_row(check: dict) -> pn.ui.Row:
        icon, color = CHECK_ICONS[check['ok']]
        return pn.ui.Row(
            pn.ui.Typography(f'<span class="material-icons" style="font-size:18px">{icon}</span>', margin=0,
                             width=22, sx={'color': color, 'display': 'flex'}),
            pn.ui.Typography(check['label'], variant='body2', width=208, margin=0),
            pn.ui.Typography(check['detail'], variant='caption', margin=0, sizing_mode='stretch_width',
                             sx={'color': 'text.secondary'}),
            margin=0, sizing_mode='stretch_width', sx={'gap': '8px', 'minHeight': '26px', 'alignItems': 'center'})

    # -- actions ------------------------------------------------------------

    async def _on_sync(self, event):
        self._sync.loading = True
        try:
            info = await asyncio.to_thread(prs.sync, self.store.cfg)
            self._notify(f"Fetched {info['total']} open pull requests", 'success')
        except Exception as e:
            self._notify(f'PR sync failed: {e}', 'error')
        finally:
            self._sync.loading = False
        self.reload()

    async def _on_start(self, event):
        if self._running:
            return
        cfg = self.store.cfg
        if not self._pulls:
            await asyncio.to_thread(prs.sync, cfg)
            self.reload()
        pulls = prs.select(cfg, self._scope.value)
        if not pulls:
            self._notify('Nothing to review in this scope.', 'info')
            return
        self._running = True
        self._start.loading = True
        self._progress.param.update(value=0, visible=True)
        done = []

        def progress(result):
            done.append(result)
            self._progress.value = round(100 * len(done) / len(pulls))

        try:
            await prs.review_many(cfg, pulls, model=self._model.value, workers=self._workers.value, progress=progress)
            failed = sum(r.get('status') == 'error' for r in done)
            self._notify(f'Reviewed {len(done) - failed} PR(s)' + (f', {failed} failed' if failed else ''),
                         'warning' if failed else 'success')
        finally:
            self._running = False
            self._start.loading = False
            self._progress.visible = False
        self.reload()

    async def _on_draft(self, event):
        if self._current is None:
            return
        self._draft.loading = True
        try:
            self._comment.value = await prs.draft_comment(self.store.cfg, self._current)
        except Exception as e:
            self._notify(f'Could not draft a comment: {e}', 'error')
        finally:
            self._draft.loading = False
        self._results = prs.results(self.store.cfg)

    def _on_post(self, event):
        text = (self._comment.value_input or self._comment.value or '').strip()
        if self._current is None or not text:
            self._notify('Write or draft a comment first.', 'warning')
            return
        with pn.io.hold():
            self._confirm_text.object = f'**#{self._current}**\n\n' + '\n'.join(f'> {line}' for line in text.splitlines())
            self._confirm_result.objects = []
            self._confirm_go.disabled = False
            self._confirm.open = True

    async def _on_confirm(self, event):
        self._confirm_go.disabled = True
        text = (self._comment.value_input or self._comment.value or '').strip()
        result = await asyncio.to_thread(prs.post_comment, self.store.cfg, self._current, text)
        if result['ok']:
            prs.update_result(self.store.cfg, self._current, decision='changes')
        self._confirm_result.objects = [pn.ui.Alert(
            object=f"Posted: {result['output']}" if result['ok'] else f"Failed: {result['output'][:300]}",
            severity='success' if result['ok'] else 'error', sizing_mode='stretch_width', margin=0)]
        self.reload()

    def _decide(self, decision: str):
        if self._current is not None:
            prs.update_result(self.store.cfg, self._current, decision=decision)
            self.reload()
