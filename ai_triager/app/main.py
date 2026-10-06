"""Triage app: `ai-triager app` or `panel serve ai_triager/app/main.py --args --workspace <dir>`."""
from __future__ import annotations

import argparse
import os
import sys

from pathlib import Path

import panel as pn
import param

from ai_triager.app.chat_view import EXTENSIONS as CHAT_EXTENSIONS, ChatView
from ai_triager.app.close_view import CloseQueue
from ai_triager.app.issues import IssueBrowser
from ai_triager.app.labels_view import LabelsView
from ai_triager.app.prs_view import PRReview
from ai_triager.app.jobs_view import JobsView
from ai_triager.app.overview import Overview
from ai_triager.app.settings_view import SettingsView
from ai_triager.app.store import Store
from ai_triager.app.sync_view import SyncView
from ai_triager.app.theme import ASSETS, HEADER_SX, THEME
from ai_triager.app.wizard import SetupWizard
from ai_triager.config import CONFIG_NAME, find_root
from ai_triager.setup import create_workspace, incomplete

pn.extension(*dict.fromkeys(['tabulator', 'codeeditor', *CHAT_EXTENSIONS]), notifications=True, throttled=True,
             respect_explicit_sizing=True,
             css_files=['https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.2/css/all.min.css'])

NAV = [
    {'label': 'Overview', 'icon': 'dashboard'},
    {'label': 'Chat', 'icon': 'forum'},
    {'label': 'Issues', 'icon': 'fact_check'},
    {'label': 'Close review', 'icon': 'task_alt'},
    {'label': 'Labels', 'icon': 'label'},
    {'label': 'PR review', 'icon': 'rate_review'},
    {'label': 'Batches', 'icon': 'rocket_launch'},
    {'label': 'Sync', 'icon': 'sync'},
    {'label': 'Settings', 'icon': 'tune'},
]


def workspace() -> Path:
    """The workspace to open; a directory without triage.toml gets a blank one for the wizard to fill."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', default=os.environ.get('TRIAGE_WORKSPACE'))
    args, _ = parser.parse_known_args(sys.argv[1:])
    if args.workspace:
        root = Path(args.workspace).expanduser().resolve()
    else:
        try:
            root = find_root()
        except FileNotFoundError:
            root = Path.cwd()
    if not (root / CONFIG_NAME).exists():
        create_workspace(root)
    return root


def notify(msg: str, kind: str = 'info'):
    getattr(pn.state.notifications, kind)(msg, duration=4000)


class TriageApp(pn.viewable.Viewer):

    store = param.ClassSelector(class_=Store)

    def __init__(self, **params):
        super().__init__(**params)
        self._menu = pn.ui.MenuList(items=NAV, value=NAV[0], sizing_mode='stretch_width', margin=0)
        self._issues = IssueBrowser(self.store, notify)
        self._jobs = JobsView(self.store, notify)
        self._overview = Overview(store=self.store, on_open_category=self._open_category,
                                  on_open_issue=self._open_issue)
        self._sync = SyncView(store=self.store, notify=notify, on_open_issue=self._open_issue)
        self._close = CloseQueue(store=self.store, notify=notify)
        self._labels = LabelsView(store=self.store, notify=notify)
        self._prs = PRReview(store=self.store, notify=notify)
        self._chat = ChatView(store=self.store, notify=notify)
        self._settings = SettingsView(self.store, notify, on_saved=self._jobs.reset_form)
        self._views = {'Overview': self._overview, 'Issues': self._issues.panel, 'Close review': self._close,
                       'Labels': self._labels, 'PR review': self._prs, 'Chat': self._chat,
                       'Batches': self._jobs.panel,
                       'Sync': self._sync, 'Settings': self._settings.panel}
        self._content = pn.ui.Column(sizing_mode='stretch_width', margin=0)
        self._wizard = SetupWizard(self.store, notify, on_saved=self._on_setup_saved)
        missing = incomplete(self.store.cfg)
        self._dialog = pn.ui.Dialog(self._wizard, title='Set up ai-triager', open=bool(missing), width_option='lg',
                                    show_close_button=True, close_on_click=False)
        if missing:
            self._wizard.open_at_first_gap()
        setup_btn = pn.ui.Button(label='Setup guide', icon='auto_fix_high', variant='outlined', size='small',
                                 margin=(0, 0, 0, 16), sx={'color': '#ffffff', 'borderColor': 'rgba(255,255,255,0.6)',
                                                           '&:hover': {'borderColor': '#ffffff'}})
        setup_btn.on_click(self._open_setup)
        cfg = self.store.cfg
        self._repo_label = pn.ui.Typography(cfg.repo or 'no repository yet', variant='caption', margin=(8, 16),
                                            sx={'color': 'text.secondary'})
        self._page = pn.ui.Page(
            title=f'{cfg.repo} triage' if cfg.repo else 'ai-triager',
            header=[setup_btn],
            sidebar=[self._menu, self._repo_label],
            sidebar_width=200,
            main=[self._content, self._dialog],
            theme_config=THEME,
            logo=str(ASSETS / 'logo.svg'),
            favicon=str(ASSETS / 'favicon.svg'),
            sx=HEADER_SX,
        )
        self._menu.param.watch(self._show, 'value')
        self._show()
        pn.state.onload(lambda: pn.state.add_periodic_callback(self._poll, period=3000))

    def __panel__(self):
        return self._page

    def _go(self, label: str):
        self._menu.value = next(item for item in NAV if item['label'] == label)

    def _open_category(self, category: str):
        self._issues.set_category(category)
        self._go('Issues')

    def _open_issue(self, n: int):
        self._issues.show(n)
        self._go('Issues')

    def _open_setup(self, event=None):
        self._wizard.open_at_first_gap()
        self._dialog.open = True

    def _on_setup_saved(self, complete: bool):
        cfg = self.store.cfg
        with pn.io.hold():
            self._repo_label.object = cfg.repo
            self._page.title = f'{cfg.repo} triage'
            self._jobs.reset_form()
            self._settings.load()
            if complete:
                self._dialog.open = False

    def _show(self, *events):
        label = (self._menu.value or NAV[0])['label']
        self._content.objects = [self._views[label]]
        if label == 'Chat':
            self._chat.activate()

    def _poll(self):
        self.store.refresh()
        label = (self._menu.value or {}).get('label')
        if label == 'Batches':
            self._jobs.tick()
        elif label == 'Labels':
            self._labels.poll()


TriageApp(store=Store(workspace())).servable()
