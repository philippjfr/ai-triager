"""Generic GitHub issue triage harness driven by LLM agents.

A workspace is a directory with a ``triage.toml`` describing the project
(repository, local checkout, reproducer environment, categories and extra
writeup fields). Per-issue writeups in ``issues/<N>.md`` are the source of
truth; everything else is derived from them.
"""

import os

# pydantic-ai prints a banner on first use, which clutters CLI output and logs.
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')
