from __future__ import annotations

import asyncio
import html
import json
import re
import sys

from pathlib import Path

import panel as pn
import param

from .. import core, github
from .store import Store
from .theme import held, resizable

ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
TABLE_COLUMNS = ['issue', 'title', 'type', 'gh_labels', 'category', 'confidence', 'recommendation', 'verified',
                 'model', 'summary']
TYPE_ICONS = {'Bug': 'bug_report', 'Feature': 'star', 'Enhancement': 'trending_up', 'Documentation': 'menu_book'}
VERIFIED_COLORS = {'yes': 'success', 'disputed': 'error', 'no': 'default'}
RECOMMENDATIONS = ['', 'close', 'keep', 'needs-info', 'relabel', 'escalate']


def fence(text: str, lang: str = '') -> str:
    ticks = '````' if '```' in text else '```'
    return f'{ticks}{lang}\n{text}\n{ticks}'


def render_events(path) -> str:
    """Render an agent event log (JSONL) as markdown with collapsible tool results."""
    out = []
    for line in path.read_text().splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind, t = ev.get('kind'), ev.get('t', 0)
        if kind == 'start':
            prompt = '\n'.join(f'> {line}' for line in ev.get('prompt', '').splitlines())
            out.append(f"**{ev.get('model')}** as `{ev.get('agent')}`\n\n{prompt}")
        elif kind == 'text':
            out.append(f"<small>{t}s</small> {ev['content']}")
        elif kind == 'call':
            args = ev.get('args') or {}
            if ev.get('tool') == 'bash':
                body = fence(args.get('command', ''), 'bash')
            elif ev.get('tool') in ('write_file', 'edit_file'):
                body = f"`{args.get('path')}`\n\n" + fence(args.get('content') or args.get('new') or '')
            else:
                body = fence(json.dumps(args, indent=1), 'json')
            out.append(f"<small>{t}s</small> **{ev.get('tool')}**\n\n{body}")
        elif kind == 'result':
            content = html.escape(str(ev.get('content', '')))
            first = content.splitlines()[0][:100] if content else ''
            out.append(f'<details><summary><small>result: {first}</small></summary><pre>{content}</pre></details>')
        elif kind == 'end':
            usage = ev.get('usage') or {}
            cost = f", ${ev['cost']:.3f}" if ev.get('cost') is not None else ''
            out.append(f"**Ended: {ev.get('status')}** after {ev.get('duration')}s, "
                       f"{usage.get('requests', '?')} requests{cost}"
                       + (f"\n\n{fence(ev['error'])}" if ev.get('error') else ''))
    return '\n\n'.join(out) or 'No events recorded.'


class IssueBrowser(param.Parameterized):

    categories = param.ListSelector(default=[])
    issue = param.Integer(default=None, allow_None=True)

    def __init__(self, store: Store, notify, **params):
        super().__init__(**params)
        self.store = store
        self.notify = notify
        cfg = store.cfg
        cats = list(cfg.categories) + ['pending']
        self.param.categories.objects = cats

        self.category_filter = pn.ui.MultiChoice(label='Category', options=cats, sizing_mode='stretch_width')
        self.recommendation_filter = pn.ui.MultiChoice(label='Recommendation', options=RECOMMENDATIONS[1:],
                                                      sizing_mode='stretch_width')
        self.verified_filter = pn.ui.Select(label='Review state', options=['any', 'unreviewed', 'yes', 'disputed'],
                                           value='any', width=150)
        self.model_filter = pn.ui.Select(label='Model', options=['any'], value='any', width=220)
        self.open_only = pn.ui.Switch(label='Open only', value=True, align='center')
        self.search = pn.ui.TextInput(label='Search', placeholder='title, summary or #number',
                                     sizing_mode='stretch_width')
        for w in (self.category_filter, self.recommendation_filter, self.verified_filter, self.model_filter,
                  self.open_only):
            w.param.watch(lambda e: self._filter(), 'value')
        self.search.param.watch(lambda e: self._filter(), 'value_input')

        self.table = pn.ui.Tabulator(
            show_index=False, disabled=True, selectable=1, pagination=None, theme='materialize',
            sizing_mode='stretch_both', min_height=300, layout='fit_columns',
            widths={'issue': 60, 'title': 240, 'type': 105, 'gh_labels': 170, 'category': 120, 'confidence': 70,
                    'recommendation': 80, 'verified': 75, 'model': 150, 'summary': 300},
            titles={'recommendation': 'rec', 'confidence': 'conf', 'gh_labels': 'labels'},
            header_tooltips={'summary': 'One-line finding from the writeup'},
            formatters={'issue': {'type': 'plaintext'}, 'title': {'type': 'textarea'},
                        'summary': {'type': 'textarea'}, 'gh_labels': {'type': 'textarea'}},
        )
        self.table.param.watch(self._on_select, 'selection')
        self.count = pn.ui.Typography('', variant='body2', margin=(0, 10))

        # Detail
        self.heading = pn.ui.Typography('Select an issue', variant='h6', margin=(0, 0, 4, 0))
        self.path_label = pn.ui.Typography('', variant='caption', margin=(0, 0, 4, 0))
        self._loaded_text = None
        self.chips = pn.ui.Row(margin=0)
        self.gh_chips = pn.ui.Row(margin=0, sx={'flexWrap': 'wrap', 'rowGap': '4px', 'alignItems': 'center'})
        self.github_link = pn.ui.Button(label='GitHub', icon='open_in_new', variant='outlined', size='small',
                                       target='_blank', visible=False)
        self.prev_btn = pn.ui.Button(icon='chevron_left', label='Prev', size='small', variant='text')
        self.next_btn = pn.ui.Button(end_icon='chevron_right', label='Next', size='small', variant='text')
        self.prev_btn.on_click(lambda e: self._step(-1))
        self.next_btn.on_click(lambda e: self._step(1))

        self.writeup_md = pn.ui.Markdown(sizing_mode='stretch_width', disable_anchors=True)
        self.fields_md = pn.ui.Markdown(sizing_mode='stretch_width')
        self.issue_md = pn.ui.Markdown(sizing_mode='stretch_width', disable_anchors=True)
        self.fetch_btn = pn.ui.Button(label='Fetch from GitHub', icon='cloud_download', variant='outlined',
                                     visible=False)
        self.fetch_btn.on_click(self._fetch)
        self.repro_area = pn.ui.Column(sizing_mode='stretch_width')
        self.transcript_select = pn.ui.Select(label='Session', options=[], width=320)
        self.transcript_select.param.watch(self._show_transcript, 'value')
        self.transcript_md = pn.ui.Markdown(sizing_mode='stretch_width', disable_anchors=True)
        self.editor = pn.ui.CodeEditor(language='markdown', sizing_mode='stretch_both', min_height=400,
                                       soft_tabs=True, on_keyup=True)
        self.edit_status = pn.ui.Column(sizing_mode='stretch_width', margin=0)
        save = pn.ui.Button(label='Save', icon='save', color='primary')
        validate = pn.ui.Button(label='Validate', icon='rule', variant='outlined')
        revert = pn.ui.Button(label='Revert', icon='undo', variant='text')
        save.on_click(self._save_edit)
        validate.on_click(self._validate_edit)
        revert.on_click(lambda e: self._load_editor())

        def pane(*objects, scroll=True):
            # Each tab fills the space below the header and scrolls on its own.
            return pn.ui.Column(*objects, sizing_mode='stretch_both', scroll=scroll, margin=(8, 0, 0, 0))

        self.tabs = pn.ui.Tabs(
            ('Writeup', pane(self.fields_md, self.writeup_md)),
            ('Issue', pane(self.fetch_btn, self.issue_md)),
            ('Reproducer', pane(self.repro_area)),
            ('Agent transcript', pane(self.transcript_select, self.transcript_md)),
            ('Edit', pane(pn.ui.Row(save, validate, revert, margin=0), self.edit_status, self.editor, scroll=False)),
            sizing_mode='stretch_both', dynamic=False, margin=0,
        )

        # Review bar
        self.r_category = pn.ui.Select(label='Category', options=list(cfg.categories), width=160, size='small')
        self.r_confidence = pn.ui.Select(label='Confidence', options=['low', 'medium', 'high'], width=115,
                                         size='small')
        self.r_recommendation = pn.ui.Select(label='Recommendation', options=RECOMMENDATIONS[1:], width=145,
                                             size='small')
        self.r_note = pn.ui.TextAreaInput(label='Review note (appended to the writeup)', rows=1, auto_grow=True,
                                          max_rows=4, sizing_mode='stretch_width', size='small')
        apply_btn = pn.ui.Button(label='Save fields', icon='edit', variant='text', size='small')
        verify_btn = pn.ui.Button(label='Verify', icon='check_circle', color='success', size='small')
        dispute_btn = pn.ui.Button(label='Dispute', icon='report', color='error', variant='outlined', size='small')
        apply_btn.on_click(self._apply_fields)
        verify_btn.on_click(lambda e: self._verdict('yes'))
        dispute_btn.on_click(lambda e: self._verdict('disputed'))
        self.review_card = pn.ui.Paper(
            pn.ui.Row(self.r_category, self.r_confidence, self.r_recommendation, apply_btn, pn.ui.HSpacer(),
                      verify_btn, dispute_btn, align='center', sizing_mode='stretch_width', margin=0,
                      sx={'gap': '4px', 'flexWrap': 'wrap'}),
            self.r_note,
            variant='outlined', sizing_mode='stretch_width', margin=0, visible=False,
            sx={'p': 1.5, 'display': 'flex', 'flexDirection': 'column', 'gap': '4px', 'bgcolor': 'background.default'},
        )

        filters = pn.ui.Column(
            pn.ui.Row(self.search, self.open_only, sizing_mode='stretch_width'),
            pn.ui.Row(self.category_filter, self.recommendation_filter, sizing_mode='stretch_width'),
            pn.ui.Row(self.verified_filter, self.model_filter, self.count, sizing_mode='stretch_width'),
            sizing_mode='stretch_width',
        )
        detail = pn.ui.Paper(
            pn.ui.Row(self.prev_btn, self.next_btn, pn.ui.HSpacer(), self.github_link, align='center',
                      sizing_mode='stretch_width', margin=0),
            self.heading, self.path_label, self.chips, self.gh_chips, self.review_card, self.tabs,
            variant='outlined', sizing_mode='stretch_both', margin=0,
            sx={'p': 2, 'display': 'flex', 'flexDirection': 'column', 'gap': '6px', 'minHeight': 0},
        )
        # The page fills the viewport below the header; the table and each tab scroll inside it.
        self.panel = resizable(
            pn.ui.Column(filters, self.table, sizing_mode='stretch_both', margin=0),
            detail, sizes=(45, 55), min_size=(420, 520), height='calc(100vh - 92px)', margin=10,
        )
        store.param.watch(self._on_store, 'version')
        self._on_store()

    # -- table --------------------------------------------------------------

    def _on_store(self, *events):
        models = sorted(m for m in self.store.df['model'].dropna().unique() if m)
        self.model_filter.options = ['any'] + models
        self._filter()
        if self.issue is None or not events:
            return
        path = core.writeup_path(self.store.cfg, self.issue)
        on_disk = path.read_text() if path.exists() else ''
        if on_disk == self._loaded_text:
            return
        if self.editor.value != self._loaded_text:
            # Unsaved edits in the app: keep them and let the user decide.
            self.edit_status.objects = [pn.ui.Alert(
                object=f'`{path.relative_to(self.store.cfg.root)}` changed on disk while you were editing. '
                       'Save to overwrite it, or Revert to load the new version.',
                alert_type='warning', sizing_mode='stretch_width')]
            return
        self._show(self.issue)

    def _filter(self):
        df = self.store.df
        q = (self.search.value_input or '').strip().lower()
        by_number = q.lstrip('#').isdigit()
        if self.open_only.value and not by_number:
            df = df[df['open']]
        if self.category_filter.value:
            df = df[df['category'].isin(self.category_filter.value)]
        if self.recommendation_filter.value:
            df = df[df['recommendation'].isin(self.recommendation_filter.value)]
        state = self.verified_filter.value
        if state == 'unreviewed':
            df = df[df['verified'] == 'no']
        elif state in ('yes', 'disputed'):
            df = df[df['verified'] == state]
        if self.model_filter.value != 'any':
            df = df[df['model'] == self.model_filter.value]
        if q:
            if by_number:
                df = df[df['issue'].astype(str).str.startswith(q.lstrip('#'))]
            else:
                text = (df['title'].fillna('') + ' ' + df['summary'].fillna('')).str.lower()
                df = df[text.str.contains(q, regex=False)]
        self.table.value = df[TABLE_COLUMNS].reset_index(drop=True)
        self.count.object = f'{len(df)} writeups'

    def set_category(self, category: str):
        self.category_filter.value = [category]
        self.open_only.value = False

    def _on_select(self, event):
        if event.new:
            row = self.table.value.iloc[event.new[0]]
            self.show(int(row['issue']))

    def _step(self, delta: int):
        issues = list(self.table.value['issue'])
        if not issues:
            return
        idx = issues.index(self.issue) + delta if self.issue in issues else 0
        idx = max(0, min(idx, len(issues) - 1))
        self.table.selection = [idx]

    # -- detail -------------------------------------------------------------

    def show(self, n: int):
        self._show(n)

    @held
    def _show(self, n: int, keep_tab: bool = False):
        self.issue = n
        cfg = self.store.cfg
        path = core.writeup_path(cfg, n)
        if not path.exists():
            self.heading.object = f'#{n}: no writeup'
            return
        text = path.read_text()
        try:
            meta, body = core.parse_text(text, path.name)
        except ValueError as e:
            meta, body = {}, f'**Could not parse frontmatter:** {e}\n\n{fence(text)}'
        self.heading.object = f"#{n} {meta.get('title', '')}"
        self.path_label.object = (f'`{path.relative_to(cfg.root)}` is the source of truth; edit it here '
                                  'or in any editor.')
        chips = [('category', 'primary', 'filled'), ('confidence', 'primary', 'outlined'),
                 ('recommendation', 'secondary', 'outlined')]
        objs = [pn.ui.Chip(label=str(meta.get(k)), color=c, variant=v, size='small', margin=(0, 4, 0, 0))
                for k, c, v in chips if meta.get(k)]
        verified = meta.get('verified') or 'no'
        objs.append(pn.ui.Chip(label=f"verified: {verified}" + (f" by {meta['verified_by']}" if meta.get('verified_by') else ''),
                              color=VERIFIED_COLORS.get(verified, 'default'), size='small', variant='outlined',
                              icon='check' if verified == 'yes' else None))
        if meta.get('issue') not in self.store.open_numbers:
            objs.append(pn.ui.Chip(label='closed upstream', size='small', variant='outlined'))
        self.chips.objects = objs
        issue = self.store.issue_index.get(n, {})
        github = [pn.ui.Typography('On GitHub', variant='caption', margin=(0, 4, 0, 0),
                                   sx={'color': 'text.secondary'})]
        github.append(pn.ui.Chip(label=issue.get('type') or 'no type', icon=TYPE_ICONS.get(issue.get('type')),
                                 size='small', margin=(0, 4, 0, 0),
                                 variant='filled' if issue.get('type') else 'outlined'))
        github += [pn.ui.Chip(label=label, size='small', variant='outlined', margin=(0, 4, 0, 0))
                   for label in issue.get('labels', [])]
        if not issue.get('labels'):
            github.append(pn.ui.Typography('no labels', variant='caption', margin=0, sx={'color': 'text.secondary'}))
        self.gh_chips.objects = github
        self.github_link.href = meta.get('url') or f'https://github.com/{cfg.repo}/issues/{n}'
        self.github_link.visible = True

        hidden = {'issue', 'title', 'url', 'summary', 'category', 'confidence', 'recommendation', 'verified',
                  'verified_by'}
        rows = [f"| `{k}` | {html.escape(core.dump_value(v)).replace('|', '&#124;')} |"
                for k, v in meta.items() if k not in hidden and v not in (None, '', [])]
        self.fields_md.object = (f"**{meta.get('summary') or ''}**\n\n| field | value |\n|---|---|\n" + '\n'.join(rows))
        self.writeup_md.object = body

        self.review_card.visible = meta.get('category') not in (None, 'pending')
        if self.review_card.visible:
            self.r_category.value = meta.get('category') if meta.get('category') in cfg.categories else None
            self.r_confidence.value = meta.get('confidence')
            self.r_recommendation.value = meta.get('recommendation')
            self.r_note.value = ''

        self._show_issue(n)
        self._show_repros(n)
        sessions = self.store.transcripts(n)
        self.transcript_select.options = {label: str(p) for label, p in sessions.items()}
        self.transcript_select.value = str(next(iter(sessions.values()))) if sessions else None
        if not sessions:
            self.transcript_md.object = 'No agent session recorded for this issue.'
        self._load_editor()

    @held
    def _show_issue(self, n: int):
        data = self.store.issue_data(n)
        self.fetch_btn.visible = data is None
        if data is None:
            self.issue_md.object = 'This issue has not been fetched yet (agents fetch it via `./triage.py context`).'
            return
        out = [f"**@{data['user']}** opened {data['created_at'][:10]}, state **{data['state']}**, "
               f"labels: {', '.join(data['labels']) or 'none'}", '', data['body'] or '_No description._']
        for c in data['comments']:
            out += ['', '---', '', f"**@{c['user']}** ({c['association'].lower()}) on {c['created_at'][:10]}",
                    '', c['body'] or '']
        refs = github.timeline_refs(self.store.cfg, data)
        if refs:
            out += ['', '---', '', '**Timeline references**', '', *refs]
        self.issue_md.object = '\n'.join(out)

    async def _fetch(self, event):
        self.fetch_btn.loading = True
        try:
            await asyncio.to_thread(github.fetch_issue, self.store.cfg, self.issue, True)
            self._show_issue(self.issue)
        except core.TriageError as e:
            self.notify(str(e), 'error')
        finally:
            self.fetch_btn.loading = False

    def _show_repros(self, n: int):
        files = self.store.repro_files(n)
        items = []
        run_btn = pn.ui.Button(label=f'Run repros/{n}.py', icon='play_arrow', variant='outlined', size='small')
        browse_btn = pn.ui.Button(label=f'Browse repros/{n}_app.py', icon='travel_explore', variant='outlined',
                                 size='small')
        names = {f.name for f in files}
        run_btn.visible = f'{n}.py' in names
        browse_btn.visible = f'{n}_app.py' in names and bool(self.store.cfg.repro.get('browse_harness'))
        run_btn.on_click(lambda e: self._run_repro('run', run_btn))
        browse_btn.on_click(lambda e: self._run_repro('browse', browse_btn))
        items.append(pn.ui.Row(run_btn, browse_btn))
        if not files:
            items.append(pn.ui.Typography('No reproducer files.', variant='body2'))
        for f in files:
            if f.suffix == '.png':
                content = pn.ui.PNG(str(f), sizing_mode='scale_width', max_width=900)
            elif f.suffix in ('.py', '.log') or f.name.endswith('.server.log'):
                text = f.read_text(errors='replace')
                content = pn.ui.CodeEditor(value=text[-200_000:], language='python' if f.suffix == '.py' else 'text',
                                                readonly=True, sizing_mode='stretch_width',
                                                height=min(500, 20 * (text.count('\n') + 2)))
            else:
                continue
            items.append(pn.ui.Card(content, title=f.name, collapsed=f.suffix == '.log' and 'server' in f.name,
                                   sizing_mode='stretch_width', margin=(0, 0, 8, 0)))
        self.repro_area.objects = items

    async def _run_repro(self, kind: str, button):
        button.loading = True
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'ai_triager', '--workspace', str(self.store.cfg.root), kind, str(self.issue),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            self.notify(f'{kind} #{self.issue} exited with {proc.returncode}',
                        'success' if proc.returncode == 0 else 'warning')
        finally:
            button.loading = False
        self._show_repros(self.issue)

    def _show_transcript(self, event):
        if not event.new:
            return
        path = Path(event.new)
        if path.suffix == '.jsonl':
            self.transcript_md.object = render_events(path)
            return
        lines = ANSI.sub('', path.read_text(errors='replace')).splitlines()
        shown = lines[-3000:]
        note = f'Showing the last {len(shown)} of {len(lines)} lines. ' if len(lines) > len(shown) else ''
        self.transcript_md.object = (f'{note}An external runner triaged several issues in this session; '
                                     f'search for `#{self.issue}`.\n\n' + fence('\n'.join(shown)))

    # -- editing and review -------------------------------------------------

    def _load_editor(self):
        self._loaded_text = self.store.writeup_text(self.issue)
        self.editor.value = self._loaded_text
        self.edit_status.objects = []

    def _report(self, errors: list[str], ok: str):
        if errors:
            self.edit_status.objects = [pn.ui.Alert(object='\n'.join(f'- {e}' for e in errors), alert_type='warning',
                                                   title='Validation', sizing_mode='stretch_width')]
        else:
            self.edit_status.objects = [pn.ui.Alert(object=ok, alert_type='success', sizing_mode='stretch_width')]

    def _validate_edit(self, event):
        text = self.editor.value
        name = f'{self.issue}.md'
        try:
            final = core.parse_text(text, name)[0].get('category') not in (None, '', 'pending')
        except ValueError:
            final = True
        self._report(core.validate_text(self.store.cfg, text, name, require_final=final), 'Valid.')

    def _save_edit(self, event):
        self._loaded_text = self.editor.value
        errors = self.store.save_writeup(self.issue, self.editor.value)
        self._report(errors, 'Saved and valid.')
        if not any('frontmatter' in e for e in errors):
            self.notify(f'Saved #{self.issue}', 'success')

    def _apply_fields(self, event):
        fields = {'category': self.r_category.value, 'confidence': self.r_confidence.value,
                  'recommendation': self.r_recommendation.value}
        errors = self.store.update_fields(self.issue, **{k: v for k, v in fields.items() if v})
        if errors:
            self.notify('Saved with validation errors: ' + '; '.join(errors[:3]), 'warning')
        else:
            self.notify(f'Updated #{self.issue}', 'success')

    def _verdict(self, result: str):
        cfg = self.store.cfg
        fields = {'category': self.r_category.value, 'confidence': self.r_confidence.value,
                  'recommendation': self.r_recommendation.value}
        meta, _ = core.parse_writeup(core.writeup_path(cfg, self.issue))
        changed = {k: v for k, v in fields.items() if v and meta.get(k) != v}
        note = self.r_note.value.strip()
        if changed:
            self.store.update_fields(self.issue, **changed)
            summary = ', '.join(f'{k} {meta.get(k)} -> {v}' for k, v in changed.items())
            note = f'{summary}. {note}'.strip()
        if result == 'disputed' and not note:
            self.notify('Add a note explaining the dispute.', 'warning')
            return
        n = self.issue
        before = list(self.table.value['issue'])
        pos = before.index(n) if n in before else -1
        core.set_verdict(cfg, n, result, self.store.reviewer(), note or None)
        self.store.refresh(force=True)
        self.notify(f"#{n} {'verified' if result == 'yes' else 'disputed'}", 'success')
        after = list(self.table.value['issue'])
        # The reviewed row may drop out of a filtered view; the next row then takes its position.
        nxt = pos + 1 if n in after else pos
        if 0 <= nxt < len(after):
            self.table.selection = [nxt]
