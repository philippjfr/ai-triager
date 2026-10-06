# CLI reference

Inside a workspace run commands as `./triage.py <command>`; elsewhere use `ai-triager --workspace DIR <command>`. Every command has `--help`.

## Setup

| Command | What it does |
|---|---|
| `init [DIR]` | Set up a workspace (`--repo`, `--checkout`, `--python`, `--skills`, `--name`, `--package`) |
| `check` | Show what is configured and what is missing |
| `doctor` | Check gh, the project environment and the browser pipeline |
| `sandbox` | Probe the execution sandbox and the guard |
| `keys` | Show which providers have keys; `--set VAR` stores one |
| `skills` | List skills available to the built-in agent |
| `components` | Regenerate the component map |
| `app` | Serve the web app (`--show`, `--port`, `--dev`) |

## Issues

| Command | What it does |
|---|---|
| `sync` | Fetch the open issue list (`--details`, `--stale`, `--issue N`) |
| `stale` | List writeups whose issue changed after triage |
| `context N` | Print an issue's body, comments, cross-references and hints |
| `next` | Claim the next issues and scaffold writeups (`--agent`, `--review`, `--issue`, `--label`, `--order`) |
| `run N` | Run `repros/N.py` with the guard, sandbox and a timeout |
| `browse N` | Serve `repros/N_app.py` and inspect it in headless Chromium |
| `validate [N...]` | Validate writeups |
| `finish N` | Stamp, validate, release the claim and reindex |
| `verify N --result yes\|disputed` | Record a review verdict |
| `release N` | Drop claims |
| `index` | Regenerate `INDEX.md` |

## Batches

| Command | What it does |
|---|---|
| `launch` | Start a background triage (or `--review`) job: `-m MODEL`, `-c COUNT`, `-j WORKERS`, `--runner`, `--issue`, `--label`, `--effort`, `--steps` |
| `jobs` | List jobs |
| `stop JOB` | Stop a running job |

## Closing

| Command | What it does |
|---|---|
| `closing` | List issues recommended for closing and their decisions |
| `decide N accept\|rereview\|keep\|clear` | Record a decision (`--note`) |
| `rereview [N...]` | Send re-review decisions back to a reviewer (`--launch`, `--model`) |
| `close [N...]` | Comment on and close accepted issues; a dry run unless `--yes` |

## Labels

| Command | What it does |
|---|---|
| `labels sync` | Fetch the label catalogue and issue types |
| `labels classify` | Review labels (`--scope`, `--count`, `--model`, `--workers`, `--issue`, `--background`) |
| `labels list` | Show suggestions (`--all` includes applied and dismissed) |
| `labels apply --issue N` | Apply suggestions; a dry run unless `--yes` |

## Pull requests

| Command | What it does |
|---|---|
| `prs sync` | Fetch open pull requests |
| `prs review [N...]` | Review PRs (`--scope unreviewed\|external\|all`, `--limit`, `--workers`, `--model`) |
| `prs list` | Show results with failed checks |
| `prs comment N` | Draft a comment for the contributor (`--model`) |
