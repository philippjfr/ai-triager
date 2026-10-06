# ai-triager

LLM-driven triage of open GitHub issues, labels and pull requests. Agents claim issues, reproduce them against a local checkout, and record a diagnosis per issue in `issues/<N>.md`. Humans or stronger models then review those writeups. Agents never write to GitHub; closing issues, changing labels and posting PR comments only happen when a maintainer confirms them.

Full documentation lives in `docs/` and builds with [Zensical](https://zensical.org): `pip install -e '.[docs]' && zensical serve`.

## Setup

```bash
# A venv layered on an env that already has Panel and panel-material-ui (here dev3.14)
~/miniconda3/envs/dev3.14/bin/python -m venv --system-site-packages .venv
.venv/bin/pip install -e '.[agent]'
```

The CLI is stdlib-only. The `agent` extra adds pydantic-ai for the built-in runner. The app also needs Panel and panel-material-ui, either inherited from the base env as above or installed with the `app` extra.

## Workspaces

Point `ai-triager init` at a directory to set one up:

```bash
ai-triager init ~/development/param-triage --checkout ~/development/param --skills ~/development/panel/.claude/skills
cd ~/development/param-triage && ./triage.py sync && ./triage.py doctor
```

The GitHub repo is inferred from the checkout's git remote. If you run `ai-triager init .triage` inside a project checkout, that checkout is used. A workspace looks like this:

```
triage.toml        project definition: repo, checkout, env, categories, fields, rules, skills, agent defaults
AGENTS.md          the procedure agents follow; NOTES.md collects reusable techniques
issues/<N>.md      one writeup per issue, the source of truth; edit in any editor or in the app
repros/<N>*.py     reproducers, their logs and screenshots
INDEX.md           generated overview of all writeups
skills/            agent skills (SKILL.md directories); more locations via [skills] paths
prompts/, templates/   role prompts and the writeup template
triage.py          workspace entry point (`./triage.py <command>`)
.triage/           machine state: issue list, caches, claims, jobs and agent transcripts
```

Project-specific knowledge lives in `triage.toml`. Its `[[rules]]` come in three stages. `validate` rules check finished writeups (`when`, plus `require` or `expect`). `scaffold` rules set fields on new writeups. `context` rules add hints next to components detected in an issue. Scaffold and context conditions can test component-map attributes with `"component.<attr>" = true`. The Panel workspace uses these for its classic-widget / panel-material-ui logic.

## Running agents

The built-in runner is a pydantic-ai agent, so any provider works with an API key: `anthropic:`, `openai:`, `google-gla:`, `openrouter:`, `groq:` and so on. Each issue gets a fresh session with five tools (shell, read-only GitHub queries, read, write, edit).

Issue text is untrusted, so every shell command is first screened by a decision model (Jev by default, `[sandbox.guard]`) and then runs in a sandbox (`[sandbox]`: macOS seatbelt or Docker) with no network, no access to credential files and writes limited to the workspace outputs. Reproducers started with `./triage.py run`/`browse` go through the same gate. `./triage.py sandbox` probes both.

```bash
ai-triager launch -m anthropic:claude-haiku-4-5 -c 20 -j 3        # triage 20 issues, 3 in parallel
ai-triager launch -r -m anthropic:claude-sonnet-5-5 -c 10          # review pass
ai-triager launch --runner kilo -m kilo/z-ai/glm-5.3-flash -c 10   # external CLI runner
ai-triager jobs; ai-triager stop <job-id>
ai-triager keys --set OPENROUTER_API_KEY                           # stored in ~/.config/ai-triager, mode 600
```

Each enabled skill is offered to the agent as a deferred capability: it sees the skill names and descriptions, and loads a skill's instructions with `load_capability` when one is relevant. `./triage.py skills` lists them.

Jobs are detached processes. Their status, logs and per-issue transcripts live under `.triage/`.

## App

```bash
ai-triager app --show
```

The main pages:

- **Overview**: progress, a category breakdown (click a category to filter), per-model review agreement, issue types, active claims and validation errors.
- **Chat**: ask questions about the backlog; issues, writeups and stats are DuckDB tables for a Lumen agent.
- **Close review**, **Labels** and **PR review**: decide on close recommendations, label and issue type suggestions, and first-pass PR checks (AI policy, PR template, smell test), then act on GitHub after a confirmation.
- **Issues**: filter and search writeups. For each one you can see the rendered writeup, the cached GitHub thread, reproducers with logs and screenshots (and run them), and the agent transcript. You can edit the raw markdown with validation, or set category, confidence and recommendation, then Verify or Dispute with a note.
- **Batches**: preview and launch triage or review jobs, follow them with a live view of each agent's tool calls, and stop them.
- **Settings**: default runner and models, API keys, skills, the execution sandbox and guard, agent permissions, external runner commands, and project maintenance (sync, reindex, component map, doctor).

Run `ai-triager app` from inside a workspace, or pass `--workspace <dir>`.
