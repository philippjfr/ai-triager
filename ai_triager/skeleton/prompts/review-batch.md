Verify a batch of triage writeups following the "Review pass" section of AGENTS.md.

1. Claim the batch: `./triage.py next --review $ARGUMENTS` (claims 5 by default). If `TRIAGE_AGENT` is not set in your environment, also pass `--agent <your model id>`. If it prints `nothing left to review`, stop.
2. For each claimed writeup, reread the issue with `./triage.py context <N>`, rerun its reproducer, check every cited PR or commit, correct the writeup if needed and run `./triage.py finish <N>` to revalidate it, then record the verdict with `./triage.py verify <N> --result yes|disputed [--note "..."]`.
3. Finish with one line per issue: `#N verdict: what changed, if anything`.
