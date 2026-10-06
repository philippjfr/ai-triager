Triage a batch of {{name}} issues following AGENTS.md.

1. Claim the batch: `./triage.py next $ARGUMENTS` (claims 5 by default). If `TRIAGE_AGENT` is not set in your environment, also pass `--agent <your model id>`. If it prints `nothing left to claim`, stop.
2. Triage each claimed issue per AGENTS.md, ending each with `./triage.py finish <N>` (add `--agent <your model id>` if `TRIAGE_AGENT` is unset) until it reports `finished`.
3. Finish with one line per issue: `#N category (confidence): summary`.
