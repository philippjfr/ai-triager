# Running agents

## Triage and review

A **triage** batch claims issues that have no writeup yet, scaffolds `issues/<N>.md` and runs one fresh agent session per issue. The agent reads the issue with `./triage.py context`, looks for existing fixes in the checkout's history, writes a reproducer, runs it, picks a category and fills in the writeup, then runs `./triage.py finish`.

A **review** batch claims finished writeups that are not verified yet (closable ones first), reruns their reproducers and checks every cited PR, commit and line. The reviewer corrects the writeup if needed and records a verdict with `./triage.py verify`.

```bash
./triage.py launch -m openrouter:openai/gpt-6-luna -c 20 -j 3     # triage 20 issues, 3 in parallel
./triage.py launch -r -m openai:gpt-6-sol -c 10                    # review 10 writeups
./triage.py launch -m ... --issue 386 541                          # specific issues
./triage.py launch -m ... --label "component: layout" --order newest
./triage.py jobs
./triage.py stop <job-id>
```

Jobs are detached processes, so they keep running when you close the app or the terminal. Each job writes its status, a log and one transcript per issue under `.triage/`; the Batches page shows them live, including every tool call.

Useful options: `--effort` sets the thinking effort for models that support it, `--steps` caps model requests per issue, and `--foreground` runs in the current terminal.

## The agent's tools

The built-in agent has five tools:

| Tool | What it does |
|---|---|
| `bash` | Runs a shell command in the workspace, screened by the guard and confined by the sandbox |
| `github` | Read-only GitHub REST queries (`repos/...`, `search/...`), executed by the harness |
| `read_file` | Reads files with line numbers, including the project checkout |
| `write_file`, `edit_file` | Writes and edits files, limited to writeups, reproducers and notes |

Before each session the orchestrator refreshes the issue's body, comments and timeline into the cache, so `./triage.py context` works inside the sandbox without network access.

## Reproducers

`./triage.py run <N>` runs `repros/<N>.py` with the project's interpreter and a timeout, and writes `repros/<N>.log`. `./triage.py browse <N>` serves `repros/<N>_app.py` with `panel serve`, opens it in headless Chromium, records page errors, console output and a screenshot, and runs `repros/<N>_check.py` if it exists. Both commands pass through the [guard and the sandbox](security.md), whether an agent or you call them.

## External runners

Instead of the built-in runner you can drive an agent CLI such as Kilo Code or Claude Code. Runners are command templates in `[agents.runners]` or on the Settings page, with placeholders such as `{model}`, `{prompt}` and `{disallowed...}`.

```bash
./triage.py launch --runner kilo -m kilo/z-ai/glm-5.3-flash -c 10
```

!!! warning
    External CLIs run their own shell, which ai-triager cannot screen or sandbox. Reproducers they start through `./triage.py run` and `browse` are still guarded and sandboxed, but other commands are not. Prefer the built-in runner for untrusted repositories, or run the external CLI inside its own sandbox.

## Re-review and closing

On the close review page, or with `./triage.py decide <N> accept|rereview|keep`, you record a decision for each issue recommended for closing. `./triage.py rereview --launch` sends the re-review ones back to a reviewer with your notes, and `./triage.py close --yes` posts the accepted comments and closes the issues. Without `--yes`, `close` only prints what it would do.
