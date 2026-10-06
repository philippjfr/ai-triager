"""Onboarding wizard: repository, issues, skills, rules, models and API keys.

Opens as a dialog when `setup.incomplete()` finds required configuration missing, and from the
header's Setup button at any time. Choices are written to triage.toml on Save; two things happen
right away because later steps depend on them: the repository is saved before the first sync,
and a pasted API key goes to the private credentials file so it can be tested.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tomllib

from pathlib import Path

import panel as pn
import param

from .. import credentials, github, skills as skills_mod
from ..config import CONFIG_NAME, RULE_STAGES
from ..setup import (
    REPO_NAME, array_of_tables, display_path, gh_authenticated, github_repo, python_candidates, run_checks,
    update_config,
)
from .store import Store
from .theme import held

SOURCES = {'Environment variable': 'env', 'Paste a key': 'stored', 'Key file': 'file'}
RULE_PRESETS = {
    'Duplicates must name the original issue': {
        'when': {'category': 'duplicate'}, 'require': ['duplicate_of'],
        'message': 'duplicate requires `duplicate_of`'},
    'High-confidence fixes must cite the fix': {
        'when': {'category': 'fixed', 'confidence': 'high'}, 'require': ['fixed_by'],
        'message': 'high-confidence `fixed` requires `fixed_by` (PR, commit or release)'},
    'Upstream issues must link where they belong': {
        'when': {'category': 'upstream'}, 'require': ['related'],
        'message': 'upstream requires a `related` link to the upstream issue'},
    'Closing recommendations need high confidence': {
        'when': {'recommendation': 'close', 'confidence': 'low'}, 'require': ['never'],
        'message': 'do not recommend closing with low confidence; use `needs-human`'},
}


class _PendingKeys:
    """Stands in for a Config so key and model resolution can use settings that are not saved yet."""

    def __init__(self, keys: dict, endpoints: dict | None = None):
        self.agents = {'keys': keys, 'endpoints': endpoints or {}}


def _ok(text: str) -> pn.ui.Alert:
    return pn.ui.Alert(object=text, severity='success', sizing_mode='stretch_width', margin=0)


def _warn(text: str, severity: str = 'warning') -> pn.ui.Alert:
    return pn.ui.Alert(object=text, severity=severity, sizing_mode='stretch_width', margin=0)


def _intro(text: str) -> pn.ui.Typography:
    return pn.ui.Typography(text, variant='body2', margin=0, sx={'color': 'text.secondary'})


def _column(*objects) -> pn.ui.Column:
    return pn.ui.Column(*objects, sizing_mode='stretch_width', margin=0, sx={'gap': '12px'})


class SetupWizard(pn.viewable.Viewer):

    active_step = param.Integer(default=0)

    def __init__(self, store: Store, notify, on_saved, **params):
        self.store = store
        self._notify = notify
        self._on_saved = on_saved
        cfg = store.cfg
        agents = cfg.agents

        # Repository
        self._checkout = pn.ui.TextInput(label='Local checkout of the project', sizing_mode='stretch_width',
                                         value=str(cfg.checkout or ''), placeholder='~/development/my-project')
        self._detect = pn.ui.Button(label='Detect from git', icon='travel_explore', variant='outlined')
        self._repo = pn.ui.TextInput(label='GitHub repository', placeholder='owner/name', value=cfg.repo,
                                     sizing_mode='stretch_width')
        self._verify_repo = pn.ui.Button(label='Check on GitHub', icon='verified', variant='outlined')
        self._name = pn.ui.TextInput(label='Display name', value=cfg.project.get('name', ''), width=220)
        self._python = pn.ui.AutocompleteInput(
            label='Python that runs reproducers', restrict=False, min_characters=0, search_strategy='includes',
            options=python_candidates(cfg.checkout), value=cfg.project.get('python', ''), sizing_mode='stretch_width')
        self._packages = pn.ui.TextInput(label='Packages to record versions of (comma separated)',
                                         value=', '.join(cfg.packages), sizing_mode='stretch_width')
        self._repo_status = _column()

        # Issues
        self._sync_btn = pn.ui.Button(label='Fetch open issues', icon='cloud_download', color='primary')
        self._sync_status = _column()

        # Skills
        self._skill_paths = pn.ui.TextAreaInput(label='Skill locations (one per line)', rows=3,
                                                value='\n'.join(cfg.skills.get('paths', [])),
                                                sizing_mode='stretch_width')
        self._skill_suggestions = pn.ui.Row(margin=0, sx={'gap': '8px', 'flexWrap': 'wrap'})
        self._find_skills = pn.ui.Button(label='Find skills', icon='search', variant='outlined')
        self._skill_list = _column()
        self._skill_switches: dict[str, pn.ui.Switch] = {}
        self._disabled_skills = set(cfg.skills.get('disabled', []))

        # Rules
        self._categories = pn.ui.Tabulator(
            pd_frame(cfg.categories), show_index=False, sizing_mode='stretch_width', height=260,
            theme='materialize', layout='fit_columns', widths={'name': 170}, margin=0,
            editors={'name': {'type': 'input'}, 'description': {'type': 'input'}},
        )
        self._add_category = pn.ui.Button(label='Add category', icon='add', variant='text', size='small')
        self._remove_category = pn.ui.Button(label='Remove selected', icon='delete', variant='text', size='small')
        effective_rules = cfg.raw.get('rules') or [r for stage in RULE_STAGES for r in cfg.rules(stage)]
        self._rules = pn.ui.CodeEditor(value=rules_toml(effective_rules), language='toml', height=260,
                                       sizing_mode='stretch_width', margin=0)
        self._preset = pn.ui.Select(label='Add a preset rule', options=[''] + list(RULE_PRESETS), value='',
                                    width=340)
        self._rules_status = _column()

        # Models and keys
        self._endpoints = {k: dict(v) for k, v in (agents.get('endpoints') or {}).items()}
        provider_options = self._provider_options()
        self._roles = {}
        for role, hint in (('triage', 'Does the first pass on every issue; a fast, inexpensive model works well.'),
                           ('review', 'Double-checks writeups before you act on them; worth a stronger model.'),
                           ('decision', 'Makes quick calls such as which labels fit an issue; a decision model '
                                        'such as Jev gives calibrated answers.'),
                           ('chat', 'Answers questions about your triage data on the Chat page.')):
            model = agents.get(f'{role}_model') or ''
            default = 'jev' if role == 'decision' and credentials.available('jev', cfg) else 'anthropic'
            provider = credentials.provider_of(model) or default
            if not model and role == 'decision' and provider == 'jev':
                model = 'jev:jev-latest'
            if not model and role == 'chat':
                provider, model = 'openrouter', 'openrouter:openai/gpt-6-luna'
            select = pn.ui.Select(label=f'{role.title()} provider', options=provider_options, value=provider,
                                  width=220)
            name = pn.ui.AutocompleteInput(label=f'{role.title()} model', restrict=False, min_characters=0,
                                           options=credentials.SUGGESTED_MODELS.get(provider, []),
                                           value=model.split(':', 1)[-1] if model else '',
                                           sizing_mode='stretch_width')
            self._roles[role] = (select, name, hint)
        self._key_specs = {p: dict(v) for p, v in (agents.get('keys') or {}).items()}
        self._ep_name = pn.ui.TextInput(label='Endpoint name', placeholder='e.g. jev', width=160)
        self._ep_url = pn.ui.TextInput(label='Base URL (OpenAI-compatible)', placeholder='https://…/v1',
                                       sizing_mode='stretch_width')
        self._ep_env = pn.ui.TextInput(label='Key variable', placeholder='e.g. JEV_API_KEY', width=200)
        self._ep_add = pn.ui.Button(label='Add endpoint', icon='add', variant='outlined')
        self._ep_add.on_click(self._on_add_endpoint)
        self._keys_area = _column()
        self._test_status = _column()

        # Review
        self._summary = pn.ui.Markdown(sizing_mode='stretch_width', margin=0)
        self._preview = pn.ui.CodeEditor(language='toml', readonly=True, height=260, sizing_mode='stretch_width',
                                         margin=0)
        self._checks = _column()

        self._steps = [
            ('Repository', 'source', self._repository_step, self._repository_complete),
            ('Issues', 'cloud_download', self._issues_step, self._issues_complete),
            ('Skills', 'auto_stories', self._skills_step, lambda: True),
            ('Rules', 'rule', self._rules_step, self._rules_complete),
            ('Models & keys', 'key', self._models_step, self._models_complete),
            ('Review', 'task_alt', self._review_step, lambda: True),
        ]
        self._visited = {0}
        self._stepper = pn.ui.StepperMenu(items=self._items(), active=0, non_linear=True, alternative_label=True,
                                          color='primary', sizing_mode='stretch_width', margin=(0, 0, 8, 0))
        self._content = pn.pane.Placeholder(sizing_mode='stretch_width')
        self._back = pn.ui.Button(label='Back', icon='arrow_back', variant='outlined', visible=False)
        self._next = pn.ui.Button(label='Continue', end_icon='arrow_forward', color='primary')
        self._save = pn.ui.Button(label='Save configuration', icon='save', color='primary', visible=False)
        self._layout = pn.ui.Column(
            self._stepper,
            # The step scrolls inside the dialog so the navigation buttons stay in view.
            pn.ui.Column(self._content, sizing_mode='stretch_width', margin=0,
                         sx={'minHeight': '430px', 'maxHeight': 'calc(100vh - 340px)', 'overflowY': 'auto',
                             'pr': 1}),
            pn.ui.Row(self._back, pn.ui.HSpacer(), self._next, self._save, sizing_mode='stretch_width', margin=0),
            sizing_mode='stretch_width', margin=0, sx={'gap': '8px'},
        )
        super().__init__(**params)

        self._stepper.param.watch(self._on_stepper, 'active')
        self._back.on_click(lambda e: self.param.update(active_step=self.active_step - 1))
        self._next.on_click(lambda e: self.param.update(active_step=self.active_step + 1))
        self._save.on_click(self._on_save)
        self._detect.on_click(self._on_detect)
        self._verify_repo.on_click(self._on_verify_repo)
        self._sync_btn.on_click(self._on_sync)
        self._find_skills.on_click(lambda e: self._update_skills())
        self._add_category.on_click(self._on_add_category)
        self._remove_category.on_click(self._on_remove_category)
        self._preset.param.watch(self._on_preset, 'value')
        self._rules.param.watch(lambda e: self._update_rules_status(), 'value')
        for select, name, _ in self._roles.values():
            select.param.watch(self._on_provider, 'value')
            name.param.watch(lambda e: self._refresh(), 'value')
        for widget in (self._repo, self._checkout):
            widget.param.watch(lambda e: self._refresh(), 'value')
        self._update_skill_suggestions()
        self._update_skills()
        self._update_rules_status()
        self._update_keys()
        self._update_view()

    def __panel__(self):
        return self._layout

    # -- step layouts -------------------------------------------------------

    def _repository_step(self):
        return _column(
            pn.ui.Typography('Which project are you triaging?', variant='h6', margin=0),
            _intro('Point at your local clone. Agents read its code and run reproducers against it, '
                   'but never change it.'),
            pn.ui.Row(self._checkout, self._detect, align='center', sizing_mode='stretch_width', margin=0),
            pn.ui.Row(self._repo, self._verify_repo, align='center', sizing_mode='stretch_width', margin=0),
            pn.ui.Row(self._name, self._python, sizing_mode='stretch_width', margin=0),
            self._packages, self._repo_status,
        )

    def _issues_step(self):
        gh_ok = gh_authenticated()
        return _column(
            pn.ui.Typography('Bring in the open issues', variant='h6', margin=0),
            _intro('The GitHub CLI fetches the open issue list. Reading is all it does: nothing is ever posted, '
                   'labelled or closed.'),
            _ok('The GitHub CLI is signed in.') if gh_ok else _warn(
                'The GitHub CLI is not signed in. Run `gh auth login` in a terminal, then come back.', 'error'),
            self._sync_btn, self._sync_status,
        )

    def _skills_step(self):
        return _column(
            pn.ui.Typography('Teach the agents about your project', variant='h6', margin=0),
            _intro('Skills are folders with a `SKILL.md`: notes on how your project works, how to test it, '
                   'house style. Agents see each skill\'s description and read it when it is relevant.'),
            self._skill_suggestions, self._skill_paths, self._find_skills, self._skill_list,
        )

    def _rules_step(self):
        return _column(
            pn.ui.Typography('Decide how issues get sorted', variant='h6', margin=0),
            _intro('Categories are the buckets an agent can file an issue into, tried in order. Rules check '
                   'every finished writeup, so an agent cannot, say, call something a duplicate without '
                   'naming the original.'),
            self._categories,
            pn.ui.Row(self._add_category, self._remove_category, margin=0),
            self._preset,
            self._rules, self._rules_status,
        )

    def _models_step(self):
        rows = []
        for role, (select, name, hint) in self._roles.items():
            rows += [pn.ui.Typography(hint, variant='caption', margin=0, sx={'color': 'text.secondary'}),
                     pn.ui.Row(select, name, sizing_mode='stretch_width', margin=0)]
        return _column(
            pn.ui.Typography('Pick the models and their keys', variant='h6', margin=0),
            _intro('Any provider pydantic-ai supports works. Keys never go into the workspace: they come from '
                   'an environment variable, a key file, or a private file in your home directory.'),
            *rows,
            pn.ui.Card(
                _intro('Use any service that speaks the OpenAI chat API, such as a self-hosted model or an '
                       'in-house decision model. It then appears as a provider above.'),
                pn.ui.Row(self._ep_name, self._ep_url, self._ep_env, self._ep_add, align='center', margin=0,
                          sizing_mode='stretch_width', sx={'gap': '8px'}),
                title='Custom endpoint', collapsed=not self._endpoints, collapsible=True, variant='outlined',
                sizing_mode='stretch_width', margin=0),
            self._keys_area, self._test_status,
        )

    def _review_step(self):
        self._update_review()
        return _column(
            pn.ui.Typography('Review and save', variant='h6', margin=0),
            _intro(f'This is what will be written to {CONFIG_NAME}. Comments and anything not shown stay as '
                   'they are.'),
            self._summary, self._preview, self._checks,
        )

    # -- completion ----------------------------------------------------------

    def _repository_complete(self) -> bool:
        checkout = Path(os.path.expanduser(self._checkout.value or '')).resolve() if self._checkout.value else None
        return bool(REPO_NAME.match(self._repo.value.strip())) and bool(checkout and (checkout / '.git').exists())

    def _issues_complete(self) -> bool:
        cfg = self.store.cfg
        if not cfg.issue_list.exists():
            return False
        return json.loads(cfg.issue_list.read_text()).get('repo') == self._repo.value.strip()

    def _rules_complete(self) -> bool:
        return self._parse_rules()[1] is None and bool(self._category_rows())

    def _models_complete(self) -> bool:
        pending = self._pending()
        triage_provider, triage_model = self._model('triage')
        return bool(triage_model) and credentials.available(triage_provider, pending)

    # -- navigation ----------------------------------------------------------

    def _items(self) -> list[dict]:
        items = []
        for i, (title, icon, _, complete) in enumerate(self._steps):
            item = {'label': title, 'icon': icon}
            if i != self.active_step and i in self._visited:
                item['completed' if complete() else 'error'] = True
            items.append(item)
        return items

    def _on_stepper(self, event):
        if event.new is not None and event.new != self.active_step:
            self.active_step = event.new

    @param.depends('active_step', watch=True)
    @held
    def _update_view(self):
        self._visited.add(self.active_step)
        if self._stepper.active != self.active_step:
            self._stepper.active = self.active_step
        self._content.update(self._steps[self.active_step][2]())
        self._refresh()

    @held
    def _refresh(self, *events):
        last = self.active_step == len(self._steps) - 1
        self._stepper.items = self._items()
        self._back.visible = self.active_step > 0
        self._next.visible = not last
        self._save.visible = last
        if self.active_step == 4:
            self._update_keys()

    def open_at_first_gap(self):
        """Jump to the first incomplete step, or to the start when everything is configured."""
        gaps = [i for i, (_, _, _, complete) in enumerate(self._steps) if not complete()]
        self.active_step = gaps[0] if gaps else 0

    # -- repository ----------------------------------------------------------

    def _on_detect(self, event):
        path = Path(os.path.expanduser(self._checkout.value or '.')).resolve()
        repo = github_repo(path)
        if not repo:
            self._repo_status.objects = [_warn(f'No GitHub remote found in {path}. Enter owner/name yourself.')]
            return
        project = repo.split('/')[-1]
        # A new workspace is named after its folder until the project is known; replace that placeholder.
        placeholder = {'', self.store.cfg.root.name, self.store.cfg.root.name.lower().replace('-', '_')}
        with pn.io.hold():
            self._repo.value = repo
            if self._name.value in placeholder:
                self._name.value = project
            if self._packages.value in placeholder:
                self._packages.value = project.lower().replace('-', '_')
            self._python.options = python_candidates(path)
            if not self._python.value and self._python.options:
                self._python.value = self._python.options[0]
            self._repo_status.objects = [_ok(f'Found {repo} in the `upstream`/`origin` remote.')]

    async def _on_verify_repo(self, event):
        repo = self._repo.value.strip()
        self._verify_repo.loading = True
        try:
            proc = await asyncio.to_thread(
                subprocess.run, ['gh', 'api', f'repos/{repo}', '--jq', '[.full_name, .open_issues_count] | @tsv'],
                capture_output=True, text=True)
        finally:
            self._verify_repo.loading = False
        if proc.returncode:
            self._repo_status.objects = [_warn(f'GitHub could not find {repo}: {proc.stderr.strip()[:200]}', 'error')]
        else:
            full, count = proc.stdout.strip().split('\t')
            self._repo_status.objects = [_ok(f'{full} exists ({count} open issues and pull requests).')]

    def _project_values(self) -> dict:
        root = self.store.cfg.root
        checkout = Path(os.path.expanduser(self._checkout.value)).resolve() if self._checkout.value else None
        return {
            'project.repo': self._repo.value.strip(),
            'project.name': self._name.value.strip() or self._repo.value.strip().split('/')[-1],
            'project.checkout': display_path(checkout, root) if checkout else '',
            'project.python': self._python.value_input or self._python.value or '',
            'project.packages': [p.strip() for p in self._packages.value.split(',') if p.strip()],
        }

    # -- issues ---------------------------------------------------------------

    async def _on_sync(self, event):
        if not REPO_NAME.match(self._repo.value.strip()):
            self._sync_status.objects = [_warn('Set the GitHub repository in the first step.', 'error')]
            return
        update_config(self.store.cfg.root, self._project_values())
        self.store.reload_config()
        self._sync_btn.loading = True
        self._sync_status.objects = [pn.ui.LinearProgress(variant='indeterminate', sizing_mode='stretch_width')]
        try:
            summary = await asyncio.to_thread(github.sync, self.store.cfg)
        except Exception as e:
            self._sync_status.objects = [_warn(f'Sync failed: {e}', 'error')]
            return
        finally:
            self._sync_btn.loading = False
        self.store.refresh(force=True)
        self._sync_status.objects = [_ok(f"Fetched {summary['total']} open issues from {self.store.cfg.repo}.")]
        self._refresh()

    # -- skills ---------------------------------------------------------------

    def _skill_locations(self) -> list[Path]:
        """Places skills commonly live: the checkout's agent folders, the user's, and sibling skill repos."""
        cfg = self.store.cfg
        found = []
        checkout = Path(os.path.expanduser(self._checkout.value)).resolve() if self._checkout.value else None
        candidates = [checkout / d for d in ('.claude/skills', '.agents/skills', 'skills')] if checkout else []
        candidates += [Path('~/.claude/skills').expanduser()]
        for parent in {cfg.root.parent, *([checkout.parent] if checkout else [])}:
            candidates += [p for p in sorted(parent.iterdir()) if p.is_dir() and 'skill' in p.name.lower()]
        for path in dict.fromkeys(candidates):
            if path.is_dir() and any((child / 'SKILL.md').exists() for child in path.iterdir() if child.is_dir()):
                found.append(path)
        return found

    def _update_skill_suggestions(self):
        chips = []
        for path in self._skill_locations():
            shown = display_path(path, self.store.cfg.root)
            chip = pn.ui.Chip(label=f'+ {shown}', variant='outlined', color='primary', margin=0)
            chip.on_click(lambda e, s=shown: self._add_skill_path(s))
            chips.append(chip)
        self._skill_suggestions.objects = chips

    def _add_skill_path(self, path: str):
        paths = [p for p in self._skill_paths.value.splitlines() if p.strip()]
        if path not in paths:
            self._skill_paths.value = '\n'.join(paths + [path])
            self._update_skills()

    def _skill_cfg(self):
        cfg = self.store.cfg
        cfg.settings = {**cfg.settings, 'skills': {'paths': self._paths(), 'disabled': []}}
        return cfg

    def _paths(self) -> list[str]:
        return [p.strip() for p in self._skill_paths.value.splitlines() if p.strip()]

    @held
    def _update_skills(self):
        saved = dict(self.store.cfg.settings)
        try:
            found = skills_mod.discover(self._skill_cfg())
        finally:
            self.store.cfg.settings = saved
        self._skill_switches = {}
        rows = []
        for skill in found:
            switch = pn.ui.Switch(label=skill.name, value=skill.name not in self._disabled_skills,
                                  styles={'min-width': '260px'})
            self._skill_switches[skill.name] = switch
            rows.append(pn.ui.Row(switch, pn.ui.Typography(skill.description, variant='body2', margin=0,
                                                           sizing_mode='stretch_width'),
                                  align='center', sizing_mode='stretch_width', margin=0))
        self._skill_list.objects = rows or [_intro('No skills found yet. That is fine: you can add them later.')]

    # -- rules ----------------------------------------------------------------

    def _category_rows(self) -> list[dict]:
        df = self._categories.value
        return [{'name': str(r['name']).strip(), 'description': str(r['description'] or '').strip()}
                for _, r in df.iterrows() if str(r['name']).strip()]

    def _on_add_category(self, event):
        df = self._categories.value
        self._categories.value = pd_frame({**dict(zip(df['name'], df['description'])), 'new-category': 'Describe it'})

    def _on_remove_category(self, event):
        if self._categories.selection:
            self._categories.value = self._categories.value.drop(index=self._categories.selection).reset_index(drop=True)

    def _parse_rules(self) -> tuple[list[dict], str | None]:
        try:
            rules = tomllib.loads(self._rules.value or '').get('rules', [])
        except tomllib.TOMLDecodeError as e:
            return [], f'Not valid TOML: {e}'
        for rule in rules:
            if rule.get('stage', 'validate') not in RULE_STAGES:
                return [], f"Unknown stage {rule['stage']!r}; use one of {', '.join(RULE_STAGES)}"
        return rules, None

    def _on_preset(self, event):
        if not event.new:
            return
        rules, error = self._parse_rules()
        if error:
            self._rules_status.objects = [_warn(error, 'error')]
        else:
            self._rules.value = rules_toml(rules + [RULE_PRESETS[event.new]])
        self._preset.value = ''

    def _update_rules_status(self):
        rules, error = self._parse_rules()
        self._rules_status.objects = [_warn(error, 'error') if error else _intro(f'{len(rules)} rule(s).')]
        self._refresh()

    # -- models and keys ------------------------------------------------------

    def _provider_options(self) -> dict:
        options = {label: key for key, label in credentials.LABELS.items()}
        options.update({f'{name} (custom)': name for name in self._endpoints})
        return options

    def _on_add_endpoint(self, event):
        name = self._ep_name.value.strip().lower()
        url = self._ep_url.value.strip()
        if not name or not url or ':' in name:
            self._test_status.objects = [_warn('Give the endpoint a short name and a base URL.', 'error')]
            return
        self._endpoints[name] = {'base_url': url, 'api_key_env': self._ep_env.value.strip()}
        self._key_specs[name] = {'source': 'env', 'env': self._ep_env.value.strip()}
        options = self._provider_options()
        with pn.io.hold():
            for select, _, _ in self._roles.values():
                select.options = options
            self._roles['decision'][0].value = name
            self._test_status.objects = [_ok(f'Added {name}. Pick its model name for the decision role below.')]
        self._refresh()

    def _model(self, role: str) -> tuple[str, str]:
        select, name, _ = self._roles[role]
        model = (name.value_input or name.value or '').strip()
        return select.value, f'{select.value}:{model}' if model else ''

    def _on_provider(self, event):
        for select, name, _ in self._roles.values():
            if select is event.obj:
                name.options = credentials.SUGGESTED_MODELS.get(event.new, [])
                name.value = name.options[0] if name.options else ''
        self._refresh()

    @held
    def _update_keys(self):
        providers = list(dict.fromkeys(self._model(role)[0] for role in self._roles))
        cards = []
        for provider in providers:
            default_env = credentials.PROVIDERS.get(provider) or self._endpoints.get(provider, {}).get('api_key_env')
            spec = self._key_specs.setdefault(provider, {'source': 'env', 'env': default_env})
            cards.append(self._key_card(provider, spec))
        self._keys_area.objects = cards

    def _key_card(self, provider: str, spec: dict) -> pn.ui.Paper:
        label = credentials.LABELS.get(provider, provider)
        value, where = credentials.resolve(provider, self._pending())
        status = pn.ui.Chip(label=f'Key found: {where}' if value else 'No key yet', margin=0, size='small',
                            color='success' if value else 'warning', variant='outlined',
                            icon='check_circle' if value else 'error_outline')
        source = pn.ui.RadioButtonGroup(options=SOURCES, value=spec.get('source', 'env') if spec.get('source') in
                                        SOURCES.values() else 'env', size='small', color='primary')
        env = pn.ui.TextInput(label='Variable name', value=spec.get('env') or '',
                              sizing_mode='stretch_width')
        paste = pn.ui.PasswordInput(label=f'{label} API key', placeholder='paste it here; it is stored privately',
                                    sizing_mode='stretch_width')
        store_btn = pn.ui.Button(label='Store key', icon='lock', variant='outlined')
        path = pn.ui.TextInput(label='Key file', value=spec.get('file', ''), placeholder='~/.config/keys/provider',
                               sizing_mode='stretch_width')
        test = pn.ui.Button(label='Test', icon='bolt', variant='outlined')
        fields = {'env': pn.ui.Row(env, margin=0, sizing_mode='stretch_width'),
                  'stored': pn.ui.Row(paste, store_btn, align='center', margin=0, sizing_mode='stretch_width'),
                  'file': pn.ui.Row(path, margin=0, sizing_mode='stretch_width')}
        body = pn.ui.Column(fields[source.value], margin=0, sizing_mode='stretch_width')

        def on_source(event):
            spec['source'] = event.new
            body.objects = [fields[event.new]]
            self._refresh_key_status(provider, status)

        def on_env(event):
            spec['env'] = event.new.strip()
            self._refresh_key_status(provider, status)

        def on_file(event):
            spec['file'] = event.new.strip()
            self._refresh_key_status(provider, status)

        def on_store(event):
            if paste.value:
                credentials.set_key(credentials.PROVIDERS.get(provider) or spec.get('env') or provider.upper(),
                                    paste.value.strip())
                paste.value = ''
                self._notify(f'Stored the {label} key in {credentials.PATH}', 'success')
                self._refresh_key_status(provider, status)

        source.param.watch(on_source, 'value')
        env.param.watch(on_env, 'value')
        path.param.watch(on_file, 'value')
        store_btn.on_click(on_store)
        test.on_click(lambda e: pn.state.execute(lambda: self._test(provider, test)))
        return pn.ui.Paper(
            pn.ui.Row(pn.ui.Typography(label, variant='subtitle1', margin=0), pn.ui.HSpacer(), status, test,
                      align='center', sizing_mode='stretch_width', margin=0, sx={'gap': '8px'}),
            source, body, variant='outlined', margin=0, sizing_mode='stretch_width',
            sx={'p': 2, 'display': 'flex', 'flexDirection': 'column', 'gap': '10px'},
        )

    def _pending(self) -> _PendingKeys:
        return _PendingKeys(self._key_specs, self._endpoints)

    def _refresh_key_status(self, provider: str, chip):
        value, where = credentials.resolve(provider, self._pending())
        chip.param.update(label=f'Key found: {where}' if value else 'No key yet',
                          color='success' if value else 'warning', icon='check_circle' if value else 'error_outline')
        self._stepper.items = self._items()

    async def _test(self, provider: str, button):
        model = next((m for p, m in (self._model(r) for r in self._roles) if p == provider and m), None)
        value, _ = credentials.resolve(provider, self._pending())
        if not model or not value:
            self._test_status.objects = [_warn(f'Choose a model and provide a key for {provider} first.', 'error')]
            return
        button.loading = True
        try:
            reply = await asyncio.to_thread(_ping, model, self._pending())
            self._test_status.objects = [_ok(f'{model} answered: “{reply.strip()[:80]}”')]
        except Exception as e:
            self._test_status.objects = [_warn(f'{model} failed: {type(e).__name__}: {str(e)[:300]}', 'error')]
        finally:
            button.loading = False

    # -- review and save ------------------------------------------------------

    def _values(self) -> dict:
        values = self._project_values()
        values['skills.paths'] = self._paths()
        values['skills.disabled'] = sorted(n for n, s in self._skill_switches.items() if not s.value) or []
        values['categories'] = self._category_rows()
        rules, error = self._parse_rules()
        if not error:
            # No rules at all falls back to the built-in defaults rather than switching validation off.
            values['rules'] = rules or None
        values['agents.runner'] = 'builtin'
        models = list(self.store.cfg.agents.get('models', []))
        for role in self._roles:
            provider, model = self._model(role)
            if model:
                values[f'agents.{role}_model'] = model
                if model not in models:
                    models.append(model)
        values['agents.models'] = models
        used = {self._model(r)[0] for r in self._roles}
        for provider, spec in self._key_specs.items():
            if provider in used:
                values[f'agents.keys.{provider}'] = {k: v for k, v in spec.items() if v}
        for name, endpoint in self._endpoints.items():
            values[f'agents.endpoints.{name}'] = endpoint
        return values

    def _update_review(self):
        import tomlkit

        values = self._values()
        doc = tomlkit.parse((self.store.cfg.root / CONFIG_NAME).read_text())
        preview = tomlkit.document()
        for dotted, value in values.items():
            *parents, key = dotted.split('.')
            table = preview
            for part in parents:
                table = table.setdefault(part, tomlkit.table())
            table[key] = value
        self._preview.value = tomlkit.dumps(preview)
        changed = sum(1 for dotted, v in values.items() if _get(doc, dotted) != v)
        self._summary.object = (f'**{changed} setting(s) change.** Repository `{values["project.repo"]}`, '
                                f'{len(values["categories"])} categories, {len(values.get("rules", []))} rules, '
                                f'{len(values["skills.paths"])} skill location(s), triage with '
                                f'`{values.get("agents.triage_model", "?")}`.')
        self._checks.objects = []

    def _on_save(self, event):
        root = self.store.cfg.root
        update_config(root, self._values())
        self.store.reload_config()
        self.store.refresh(force=True)
        missing = [c for c in run_checks(self.store.cfg, gh=False) if c.required and not c.ok]
        if missing:
            self._checks.objects = [_warn('Saved. Still missing: ' + '; '.join(f'{c.label} ({c.detail})'
                                                                               for c in missing))]
        else:
            self._checks.objects = [_ok('Saved. Everything needed for triage is in place.')]
        self._notify(f'Saved {CONFIG_NAME}', 'success')
        self._on_saved(complete=not missing)


def _get(doc, dotted: str):
    node = doc
    for part in dotted.split('.'):
        if not hasattr(node, 'get'):
            return None
        node = node.get(part)
    return node.unwrap() if hasattr(node, 'unwrap') else node


def _ping(model: str, pending: _PendingKeys) -> str:
    from pydantic_ai import Agent

    from .. import decisions, models

    if decisions.is_decision_model(model):
        result = asyncio.run(decisions.invoke(model, {'text': 'The sky is blue.'},
                                              {'check': decisions.noul('Is this statement about the sky?')},
                                              pending))
        return f"{result.get('model', model)} is reachable (yes with p={result['answers']['check']['noul']:.2f})"
    credentials.apply_to_environ(pending)
    agent = Agent(models.resolve(model, pending))
    return agent.run_sync('Reply with one short friendly sentence confirming you are reachable.').output


def pd_frame(categories: dict):
    import pandas as pd

    return pd.DataFrame({'name': list(categories), 'description': list(categories.values())})


def rules_toml(rules: list[dict]) -> str:
    import tomlkit

    doc = tomlkit.document()
    if rules:
        doc['rules'] = array_of_tables(rules)
    return tomlkit.dumps(doc) or '# [[rules]]\n# when = { category = "duplicate" }\n# require = ["duplicate_of"]\n'
