# Configuration reference

All project configuration lives in `triage.toml` at the workspace root. The setup guide and the Settings page edit it for you and keep your comments. Agent defaults saved from the Settings page go to `settings.json`, which overrides `[agents]`.

## `[project]`

| Key | Meaning |
|---|---|
| `name` | Display name |
| `repo` | `owner/name` on GitHub |
| `checkout` | Local clone agents read and run reproducers against; never modified |
| `python` | Interpreter that runs reproducers (usually the project's dev environment) |
| `packages` | Packages whose versions are recorded in `tested_on`; the first also records the checkout commit |
| `claim_ttl_hours` | How long a claim lasts before another agent may take the issue |

## `[repro]`

| Key | Default | Meaning |
|---|---|---|
| `timeout` | `120` | Seconds before `./triage.py run` kills a reproducer |
| `browse_timeout` | | Same for `./triage.py browse` |
| `browse_harness` | | Script that serves an app and inspects it in a browser |

## `[[categories]]`, `[fields]`, `[[rules]]`, `[components]`

See [Workspaces](workspaces.md#categories-and-fields).

## `[skills]`

| Key | Meaning |
|---|---|
| `paths` | Skill directories, or directories of skills |
| `disabled` | Skill names to hide from agents |

## `[agents]`

| Key | Meaning |
|---|---|
| `runner` | `builtin` or the name of an external runner |
| `triage_model`, `review_model` | Models for triage and review batches |
| `chat_model` | Model for the Chat page |
| `decision_model` | Default decision model for labels and PR review |
| `models` | Model choices offered in the app |
| `workers`, `per_batch`, `order` | Batch defaults |
| `reviewer` | Your reviewer id (default `human:<git user.name>`) |
| `deny` | fnmatch patterns of forbidden shell commands |
| `modes.triage`, `modes.review` | `steps` (request limit per issue) and `write_paths` (globs the file tools may write) |
| `runners.<name>` | External runner command templates |
| `endpoints.<name>` | OpenAI-compatible endpoints: `base_url`, `api_key_env` |

## `[labels]`

| Key | Default | Meaning |
|---|---|---|
| `add_threshold` | `0.75` | Probability above which a missing label is suggested |
| `remove_threshold` | `0.25` | Probability below which a current label is suggested for removal |
| `exclude` | `[]` | Labels the reviewer ignores |

## `[prs]`

| Key | Default | Meaning |
|---|---|---|
| `policy` | `prs/policy.md` | Contribution policy file |
| `template` | | PR template file; empty finds the repository's or organisation's |
| `max_open` | `2` | Open PRs per author across the organisation |
| `org` | repository owner | Organisation for the open PR count |
| `tests` | `["tests/*", "*/tests/*", ...]` | Globs that count as test files |
| `frontend` | `["*.ts", "*.css", ...]` | Globs for which screenshots are expected |
| `threshold` | `0.5` | Score threshold for "needs attention" |
| `trusted` | `["OWNER", "MEMBER", "COLLABORATOR"]` | Author associations hidden by default |
| `model` | `[agents] decision_model` | Decision model |
| `explain_model` | `[agents] review_model` | LLM that drafts comments |

## `[sandbox]`

| Key | Default | Meaning |
|---|---|---|
| `allow_all` | `false` | Turn off the sandbox, the guard and environment filtering |
| `backend` | `seatbelt` on macOS, else `docker` | `seatbelt`, `docker` or `local` |
| `env` | `[]` | Extra environment variables reproducers may see (wildcards allowed) |
| `network` | `false` | Allow outbound network |
| `image` | | Docker image with the project environment |
| `writable` | per kind | Paths that may be written |
| `secrets` | credential directories | Paths that may not be read |
| `agent.*`, `reproducer.*` | | Overrides for agent shells and reproducers |
| `guard.enabled` | `true` | Screen commands with a decision model |
| `guard.model` | `jev:jev-latest` | Guard model |
| `guard.min_safe` | `0.5` | Minimum probability that a command is safe |
| `guard.max_risk` | `0.7` | Maximum probability of exfiltration, destruction or injection |

The `TRIAGE_SANDBOX` environment variable overrides the backend for one command.
