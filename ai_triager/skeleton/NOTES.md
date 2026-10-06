# Triage notes

Reusable techniques only, one short bullet each, under the matching heading. No issue-specific findings; those belong in `issues/<N>.md`.

## Finding fixes

- `git -C <checkout> log --oneline -S 'symbol'` finds commits that added or removed a symbol; `-G 'regex'` matches changed lines.
- `gh search prs --repo {{repo}} --state closed "<keywords>"` finds PRs that did not reference the issue.
- `gh issue list -R {{repo}} --state all --search "<keywords>"` finds duplicates, including closed ones.

## Reproducers
