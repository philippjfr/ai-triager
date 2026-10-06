"""Rapid review of close recommendations: decide, then send re-reviews and closes in batches."""
from __future__ import annotations

import asyncio
import sys

import pandas as pd
import panel as pn
import param

from .. import closing, core, jobs
from .store import Store
from .theme import KPI, held, resizable

DECISION_LABELS = {'': 'undecided', 'accept': 'close', 'rereview': 're-review', 'keep': 'keep open',
                   'rereview-requested': 'sent to review'}
CONFIDENCE_COLORS = {'high': 'success', 'medium': 'warning', 'low': 'error'}


class CloseQueue(pn.viewable.Viewer):

    store = param.ClassSelector(class_=Store)

    def __init__(self, notify, **params):
        super().__init__(**params)
        self._notify = notify
        self._rows: dict[int, dict] = {}
        self._current: int | None = None

        self._kpis = {name: KPI(name) for name in ('To decide', 'Ready to close', 'For re-review', 'Keep open')}
        self._category = pn.ui.MultiChoice(label='Category', options=[], sizing_mode='stretch_width')
        self._show = pn.ui.Select(label='Show', options={'Undecided': 'undecided', 'All': 'all', 'To close': 'accept',
                                                         'To re-review': 'rereview', 'Keep open': 'keep'},
                                  value='undecided', width=160, size='small')
        self._verified_only = pn.ui.Switch(label='Verified only', value=False)
        self._table = pn.ui.Tabulator(
            show_index=False, disabled=True, selectable=1, pagination=None, theme='materialize',
            sizing_mode='stretch_both', min_height=300, layout='fit_columns', margin=0,
            widths={'issue': 70, 'category': 120, 'confidence': 90, 'verified': 80, 'decision': 110},
            formatters={'issue': {'type': 'plaintext'}, 'title': {'type': 'textarea'}},
        )

        self._heading = pn.ui.Typography('Nothing to review', variant='h6', margin=0)
        self._link = pn.ui.Button(label='GitHub', icon='open_in_new', variant='outlined', size='small',
                                  target='_blank', visible=False)
        self._chips = pn.ui.Row(margin=0, sx={'gap': '6px', 'flexWrap': 'wrap'})
        self._summary = pn.ui.Typography('', variant='body1', margin=0)
        self._evidence = pn.ui.Column(sizing_mode='stretch_width', margin=0, sx={'gap': '6px'})
        self._reviewers = pn.ui.Column(sizing_mode='stretch_width', margin=0)
        self._comment = pn.ui.TextAreaInput(label='Comment to post when closing', rows=4, auto_grow=True,
                                            max_rows=8, sizing_mode='stretch_width')
        self._note = pn.ui.TextInput(label='Note for the reviewer (optional, re-review only)', sizing_mode='stretch_width',
                                     size='small')
        self._accept = pn.ui.Button(label='Close with this comment', icon='check_circle', color='success')
        self._rereview = pn.ui.Button(label='Re-review', icon='replay', color='warning', variant='outlined')
        self._keep = pn.ui.Button(label='Keep open', icon='do_not_disturb_on', variant='text')
        self._skip = pn.ui.Button(label='Skip', icon='skip_next', variant='text')

        self._review_model = pn.ui.AutocompleteInput(label='Review with', restrict=False, min_characters=0,
                                                     width=280, size='small')
        self._send_review = pn.ui.Button(label='Send re-reviews', icon='replay', variant='outlined')
        self._send_close = pn.ui.Button(label='Close on GitHub…', icon='send', color='primary')
        self._confirm_list = pn.ui.Markdown(sizing_mode='stretch_width', margin=0)
        self._confirm_switch = pn.ui.Switch(label='I have read these comments and want them posted', value=False)
        self._confirm_go = pn.ui.Button(label='Post comments and close', icon='send', color='error', disabled=True)
        self._confirm_progress = pn.ui.LinearProgress(value=0, variant='determinate', visible=False,
                                                      sizing_mode='stretch_width')
        self._confirm_result = pn.ui.Column(sizing_mode='stretch_width', margin=0)
        self._confirm = pn.ui.Dialog(
            pn.ui.Column(
                pn.ui.Typography('Each issue below gets its comment and is closed with the reason shown. '
                                 'This writes to GitHub as you.', variant='body2', margin=0),
                self._confirm_list, self._confirm_switch, self._confirm_go, self._confirm_progress,
                self._confirm_result, sizing_mode='stretch_width', margin=0, sx={'gap': '12px'},
            ),
            title='Close issues on GitHub', open=False, width_option='md', show_close_button=True,
        )

        detail = pn.ui.Paper(
            pn.ui.Row(self._heading, pn.ui.HSpacer(), self._link, align='center', sizing_mode='stretch_width',
                      margin=0),
            self._chips, self._summary,
            pn.ui.Column(self._evidence, self._reviewers, sizing_mode='stretch_both', margin=0, scroll=True,
                         sx={'gap': '10px', 'minHeight': '140px'}),
            self._comment, self._note,
            pn.ui.Row(self._accept, self._rereview, self._keep, pn.ui.HSpacer(), self._skip, margin=0,
                      sizing_mode='stretch_width', sx={'gap': '8px', 'flexWrap': 'wrap'}),
            variant='outlined', sizing_mode='stretch_both', margin=0,
            sx={'p': 2, 'display': 'flex', 'flexDirection': 'column', 'gap': '10px'},
        )
        send_bar = pn.ui.Paper(
            pn.ui.Typography('Send decisions', variant='subtitle1', margin=0),
            pn.ui.HSpacer(), self._review_model, self._send_review, self._send_close,
            variant='outlined', margin=0, sizing_mode='stretch_width',
            sx={'p': 1.5, 'display': 'flex', 'alignItems': 'center', 'gap': '12px', 'flexWrap': 'wrap'},
        )
        self._layout = pn.ui.Column(
            pn.ui.Grid(*(pn.ui.Grid(k.card, size={'xs': 6, 'md': 3}) for k in self._kpis.values()),
                       container=True, spacing=2, margin=0, sizing_mode='stretch_width'),
            resizable(
                pn.ui.Column(pn.ui.Row(self._show, self._category, self._verified_only, align='center', margin=0,
                                       sizing_mode='stretch_width'),
                             self._table, sizing_mode='stretch_both', margin=0),
                detail, sizes=(45, 55), min_size=(400, 480), height='max(520px, calc(100vh - 360px))',
            ),
            send_bar, self._confirm,
            sizing_mode='stretch_width', margin=10, sx={'gap': '16px'},
        )

        self._table.param.watch(self._on_select, 'selection')
        for w in (self._category, self._show, self._verified_only):
            w.param.watch(lambda e: self._filter(), 'value')
        self._accept.on_click(lambda e: self._decide('accept'))
        self._rereview.on_click(lambda e: self._decide('rereview'))
        self._keep.on_click(lambda e: self._decide('keep'))
        self._skip.on_click(lambda e: self._advance())
        self._send_review.on_click(self._on_send_review)
        self._send_close.on_click(self._on_open_confirm)
        self._confirm_switch.param.watch(lambda e: setattr(self._confirm_go, 'disabled', not e.new), 'value')
        self._confirm_go.on_click(self._on_close)
        self.store.param.watch(lambda e: self.reload(), 'version')
        self.reload()

    def __panel__(self):
        return self._layout

    # -- data ---------------------------------------------------------------

    @held
    def reload(self):
        self._rows = {r['issue']: r for r in closing.candidates(self.store.cfg)}
        decisions = [r['decision'] for r in self._rows.values()]
        self._kpis['To decide'].update(f"{decisions.count(''):,}", f'of {len(self._rows)} recommended')
        self._kpis['Ready to close'].update(f"{decisions.count('accept'):,}", 'comment accepted')
        self._kpis['For re-review'].update(f"{decisions.count('rereview'):,}",
                                           f"{decisions.count('rereview-requested')} already sent")
        self._kpis['Keep open'].update(f"{decisions.count('keep'):,}")
        self._category.options = sorted({r['category'] for r in self._rows.values()})
        agents = self.store.cfg.agents
        self._review_model.options = list(agents.get('models', []))
        if not self._review_model.value:
            self._review_model.value = agents.get('review_model') or ''
        self._send_review.label = f"Send {decisions.count('rereview')} re-review(s)"
        self._send_review.disabled = not decisions.count('rereview')
        self._send_close.label = f"Close {decisions.count('accept')} on GitHub…"
        self._send_close.disabled = not decisions.count('accept')
        self._filter()
        if self._current in self._rows:
            self._show_issue(self._current)

    def _visible(self) -> list[dict]:
        rows = list(self._rows.values())
        show = self._show.value
        if show == 'undecided':
            rows = [r for r in rows if not r['decision']]
        elif show != 'all':
            rows = [r for r in rows if r['decision'] == show]
        if self._category.value:
            rows = [r for r in rows if r['category'] in self._category.value]
        if self._verified_only.value:
            rows = [r for r in rows if r['verified'] == 'yes']
        order = {'high': 0, 'medium': 1, 'low': 2}
        return sorted(rows, key=lambda r: (r['verified'] != 'yes', order.get(r['confidence'], 3), r['issue']))

    def _filter(self):
        rows = self._visible()
        self._table.value = pd.DataFrame(
            [{'issue': r['issue'], 'title': r['title'], 'category': r['category'], 'confidence': r['confidence'],
              'verified': r['verified'], 'decision': DECISION_LABELS.get(r['decision'], r['decision'])}
             for r in rows],
            columns=['issue', 'title', 'category', 'confidence', 'verified', 'decision'])
        if rows and self._current not in {r['issue'] for r in rows}:
            self._table.selection = [0]
        elif not rows:
            self._current = None
            self._heading.object = 'Nothing left in this view'

    # -- detail ---------------------------------------------------------------

    def _on_select(self, event):
        if event.new:
            self._show_issue(int(self._table.value.iloc[event.new[0]]['issue']))

    @held
    def _show_issue(self, n: int):
        self._current = n
        r = self._rows[n]
        repo = self.store.cfg.repo
        self._heading.object = f"#{n} {r['title']}"
        self._link.param.update(href=f'https://github.com/{repo}/issues/{n}', visible=True)
        chips = [pn.ui.Chip(label=r['category'], color='primary', size='small', margin=0),
                 pn.ui.Chip(label=f"{r['confidence'] or 'unknown'} confidence", size='small', margin=0,
                            color=CONFIDENCE_COLORS.get(r['confidence'], 'default'), variant='outlined')]
        if r['decision']:
            chips.append(pn.ui.Chip(label=DECISION_LABELS.get(r['decision'], r['decision']), size='small',
                                    margin=0, variant='outlined', icon='flag'))
        self._chips.objects = chips
        self._summary.object = r['summary']
        evidence = []
        if r['duplicate_of']:
            num = r['duplicate_of'].lstrip('#')
            state = 'open' if r['duplicate_open'] else 'closed or not in the open list'
            title = f": {r['duplicate_title']}" if r['duplicate_title'] else ''
            evidence.append(pn.ui.Alert(
                object=f"Duplicate of [{r['duplicate_of']}](https://github.com/{repo}/issues/{num}){title} "
                       f"({state}). The agent was {r['confidence'] or 'unsure'} about this.",
                severity='info', sizing_mode='stretch_width', margin=0))
        if r['fixed_by']:
            links = ', '.join(self._ref(ref) for ref in r['fixed_by'])
            evidence.append(pn.ui.Typography(f'**Fixed by** {links}', variant='body2', margin=0))
        self._evidence.objects = evidence
        lines = []
        for rev in r['reviewers']:
            icon = '🤖' if not str(rev['by']).startswith('human:') else '🙋'
            when = f" on {rev['when']}" if rev['when'] else ''
            lines.append(f"- {icon} **{rev['role']}** by `{rev['by']}`{when}")
        verified = {'yes': 'verified', 'disputed': 'disputed', 'no': 'not yet reviewed'}.get(r['verified'],
                                                                                            r['verified'])
        self._reviewers.objects = [pn.ui.Markdown(f'**Reviewers** ({verified})\n\n' + '\n'.join(lines),
                                                  sizing_mode='stretch_width', margin=0)]
        self._comment.value = r['comment']
        self._note.value = r['decision_note'] if r['decision'] == 'rereview' else ''

    def _ref(self, ref) -> str:
        ref = str(ref)
        repo = self.store.cfg.repo
        if ref.lstrip('#').isdigit():
            return f"[{ref}](https://github.com/{repo}/pull/{ref.lstrip('#')})"
        return f'`{ref}`'

    def _decide(self, decision: str):
        if self._current is None:
            return
        n = self._current
        note = self._note.value.strip() if decision == 'rereview' else ''
        closing.set_decision(self.store.cfg, n, decision, self.store.reviewer(), note, comment=self._comment.value)
        self._rows[n].update(decision=decision, decision_note=note, comment=self._comment.value)
        self._advance(decided=n)
        self.store.refresh(force=True)

    def _advance(self, decided: int | None = None):
        visible = [r['issue'] for r in self._visible()]
        current = self._current
        if current in visible:
            idx = visible.index(current) + 1
        else:
            idx = 0
        self._filter()
        remaining = list(self._table.value['issue'])
        if not remaining:
            return
        target = next((n for n in visible[idx:] if n in remaining and n != decided), remaining[0])
        self._table.selection = [remaining.index(target)]

    # -- sending ----------------------------------------------------------------

    def _on_send_review(self, event):
        numbers = [n for n, r in self._rows.items() if r['decision'] == 'rereview']
        model = (self._review_model.value_input or self._review_model.value or '').strip()
        if not numbers or not model:
            self._notify('Pick a review model first.', 'warning')
            return
        cfg = self.store.cfg
        closing.request_rereview(cfg, numbers, self.store.reviewer())
        job = jobs.spawn(cfg, {'mode': 'review', 'runner': cfg.agents.get('runner', 'builtin'), 'model': model,
                               'issues': numbers, 'count': len(numbers), 'workers': min(3, len(numbers))},
                         python=sys.executable)
        self._notify(f"Sent {len(numbers)} writeup(s) to {model} (job {job['id']})", 'success')
        self.store.refresh(force=True)
        self.reload()

    def _accepted(self) -> list[dict]:
        return [r for r in self._rows.values() if r['decision'] == 'accept']

    def _on_open_confirm(self, event):
        rows = self._accepted()
        lines = []
        for r in rows:
            reason = closing.CLOSE_REASONS.get(r['category'], 'not planned')
            dup = f" of {r['duplicate_of']}" if reason == 'duplicate' else ''
            first = (r['comment'].splitlines() or ['(no comment)'])[0][:110]
            lines.append(f"- **#{r['issue']}** closed as *{reason}*{dup}: {first}")
        with pn.io.hold():
            self._confirm_list.object = '\n'.join(lines)
            self._confirm_switch.value = False
            self._confirm_result.objects = []
            self._confirm_progress.visible = False
            self._confirm.open = True

    async def _on_close(self, event):
        rows = self._accepted()
        by = self.store.reviewer()
        self._confirm_go.disabled = True
        self._confirm_progress.param.update(value=0, visible=True)
        results = []
        for i, row in enumerate(rows, 1):
            results.append(await asyncio.to_thread(closing.close_issue, self.store.cfg, row, by))
            self._confirm_progress.value = 100 * i / len(rows)
        failed = [r for r in results if not r['ok']]
        summary = f'Closed {len(results) - len(failed)} of {len(results)} issue(s).'
        if failed:
            summary += ' Failed: ' + '; '.join(f"#{r['issue']} ({r['output'][:120]})" for r in failed)
        self._confirm_result.objects = [pn.ui.Alert(object=summary, severity='warning' if failed else 'success',
                                                    sizing_mode='stretch_width', margin=0)]
        await asyncio.to_thread(core.build_index, self.store.cfg)
        self.store.refresh(force=True)
        self.reload()
