# {{name}} issue triage

This directory is a harness for triaging open issues on `{{repo}}`. Each agent run works on claimed issues, tries to reproduce each one against the local checkout configured in `triage.toml` (`[project] checkout`), and records a diagnosis in `issues/<N>.md`. `INDEX.md` is generated from those files. This file is the procedure.

## Hard rules

- Never write to GitHub. No `gh issue comment`, `gh issue close`, `gh issue edit`, labels, reactions or PRs. The "Proposed GitHub comment" section is a draft for a human.
- Never modify the project checkout or any other checkout: no edits, no `git checkout`, `git stash`, `git pull`, `pip install`. Read its source freely.
- Only write inside this directory, and only to `issues/<N>.md` for issues you claimed, `repros/<N>*`, and `NOTES.md` (see below). Never edit `INDEX.md` or anything under `.triage/`; `./triage.py` maintains them.
- Run reproducers only through `./triage.py run` (and `./triage.py browse` if configured). They enforce timeouts and kill leftover processes.
- Spend at most about 15 minutes of effort per issue. If you are stuck, choose `needs-human`, write down what you tried, and move on. A clear `needs-human` is more useful than a guessed `fixed`.
- Do not invent evidence. Every PR, commit or file:line you cite must come from `./triage.py context`, `git -C <checkout> log`, `gh search`/`gh pr view` (or the `github` tool) output or code you read.

## Procedure for a batch

1. Claim: `./triage.py next -n 5 --agent <model-id>` (skip this when your task says the issue is already claimed). It prints the claimed issue numbers and creates `issues/<N>.md` with prefilled frontmatter. Work only on those issues.
2. For each claimed issue:
   1. `./triage.py context <N>` prints the body, comments, cross-referenced PRs and commits. Read all of it, especially maintainer comments near the end.
   2. Check `NOTES.md` for techniques relevant to this kind of issue.
   3. Look for an existing fix before reproducing: merged PRs in the timeline, `git -C <checkout> log --oneline -S '<symbol>'` or `--grep '<keyword>'`, and the changelog.
   4. Reproduce (see below), then choose a category using the decision order.
   5. Fill in `issues/<N>.md`: every frontmatter field and every body section. Replace all `TODO` text.
   6. `./triage.py finish <N> --agent <model-id>`. It validates the file, releases the claim and regenerates the index. If it prints errors, fix them and rerun until it reports `finished`.
3. If you learned a reusable technique, append one short bullet to `NOTES.md` under the matching heading. Do not add issue-specific findings there.

## Reproducing

- Write `repros/<N>.py` and run `./triage.py run <N>`. Make the script print a clear `BUG REPRODUCED: ...` or `NOT REPRODUCED: ...` line based on an explicit check, not on the absence of an exception alone. Output goes to `repros/<N>.log`.
- Start from the reporter's code verbatim. Only change what is necessary for it to run on the current version, and note every change in the writeup.
- Things you cannot test here (other operating systems, hardware, cloud services, large data or performance baselines): use `reproduced: untested` and usually `needs-human`, unless the code path clearly no longer exists (then `obsolete`) or a merged PR clearly fixes it (then `fixed`, confidence `medium`).

## Categories

Pick the first category in this order that applies:

1. `duplicate`: another issue (open or closed) describes the same problem. Set `duplicate_of: "#1234"`.
2. `fixed`: the reporter's code no longer shows the bug on main, or the requested feature now exists. Set `fixed_by` to PR numbers, commits or the release. `confidence: high` requires both a passing reproducer and an identified fix.
3. `obsolete`: the issue is about something that no longer exists or no longer applies.
4. `by-design`: the behaviour is intentional or documented, so the answer is an explanation or a docs pointer.
5. `upstream`: the root cause is in a dependency, the platform or the browser. Name the upstream project and link an upstream issue if one exists.
6. `confirmed`: the bug still reproduces on main. Explain the root cause if you found it, with file:line.
7. `feature-request`: an enhancement request that is still unimplemented and still makes sense.
8. `docs`: a documentation problem that still exists.
9. `not-reproducible`: you ran the reporter's code faithfully, it does not show the bug, and you found no fix.
10. `needs-info`: there is not enough information to even attempt a reproduction, and nobody provided it later.
11. `needs-human`: you could not decide or could not test.

## Frontmatter fields

Lists are JSON arrays (`["#8123", "abc1234"]`). Strings containing spaces, colons or `#` must be double quoted. Leave a field empty rather than deleting it.

- `category`, `confidence` (`low`, `medium`, `high`), `reproduced` (`yes`, `no`, `partial`, `untested`, `n/a`; `yes`/`no`/`partial` require `repro` to name the file you ran).
- `tested_on`: filled in by `finish`; do not edit.
- `components`: components involved, if the workspace configures a component map.
- `fixed_by`, `related`: lists of `"#1234"` references or commit hashes. `duplicate_of`: a single `"#1234"`.
- `recommendation`: `close` (fixed, obsolete, duplicate, by-design, not-reproducible with high confidence), `keep` (confirmed, feature-request, docs, upstream that affects users), `needs-info`, `relabel`, `escalate`.
- `summary`: one line, at most 200 characters, stating the finding.
- `triaged_by`: your model id. `verified`, `verified_by`: reserved for the review pass. Leave `verified: no`.

## Review pass

Reviewers (stronger models or humans) use `./triage.py next --review -n 5 --agent <id>`, which claims unverified writeups, closable ones first. For each: reread the issue with `./triage.py context <N>`, rerun the reproducer, and check the reasoning. If the writeup is sound, `./triage.py verify <N> --result yes --agent <id>`. If not, correct the writeup, then verify with `--result yes --note "what changed"`, or use `--result disputed --note "..."` when it needs a human.
