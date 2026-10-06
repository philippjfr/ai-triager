# The app

```bash
./triage.py app --show          # from inside a workspace
ai-triager app --workspace DIR  # from anywhere
```

The app is built with [Panel](https://panel.holoviz.org) and panel-material-ui. It reads the same files as the CLI, so you can switch between them freely: a writeup edited in your editor shows up in the app within a few seconds, and a batch launched from the CLI appears on the Batches page.

## Overview

Headline numbers (open, triaged, untouched, verified, recommended for closing, agent spend) and charts: writeups by category (click a bar to browse that category), triage progress over time, recommendations by confidence, review outcomes per model and open issues by GitHub issue type, split by whether they have been triaged. Active claims and validation errors are listed below.

![Overview](assets/screenshots/overview.png)

## Chat

Ask questions about the backlog: "which confirmed bugs involve Tabulator?", "how many feature requests were opened before 2022?", "show me issues the reviewers disputed". The chat agent (built on [Lumen](https://github.com/holoviz/lumen)) queries DuckDB tables of issues, writeups and stats, and has tools to pull up an issue's full writeup or GitHub thread. Configure the model as `chat_model` in Settings.

## Issues

A searchable, filterable table of writeups next to the selected issue. Drag the divider to resize the two sides. For each issue you see the GitHub issue type and labels, the rendered writeup, the cached GitHub thread, the reproducers with their logs and screenshots (with a button to rerun them), and the agent transcript. The Edit tab is a markdown editor that validates before saving. The review bar lets you change category, confidence and recommendation, then Verify or Dispute with a note.

![Issues](assets/screenshots/issues.png)

## Close review

Issues recommended for closing, with the category, confidence, who triaged and reviewed them and the proposed comment (editable). For each issue choose **Close with this comment**, **Re-review** (with an optional note for the reviewer) or **Keep open**. The send bar at the bottom launches re-review batches and closes the accepted issues on GitHub after a confirmation.

![Close review](assets/screenshots/close.png)

## Labels and PR review

See [Label review](labels.md) and [PR review](pr-review.md).

## Batches

Preview which issues a batch would claim, launch triage or review jobs with a model, runner, size and parallelism, follow running jobs with a live feed of tool calls, and stop them.

## Sync

Fetch the open issue list, refresh issue details, and see which writeups are stale because the issue changed after triage.

## Settings

Models for each task, API keys, skills, the execution sandbox and guard (with a **Test sandbox** button), the agent deny list and writable paths, external runner commands, and project maintenance actions. Model and agent settings are saved to `settings.json`; sandbox settings to `triage.toml`.

![Settings](assets/screenshots/settings.png)
