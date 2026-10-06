# Skills

Put agent skills here, one directory per skill with a `SKILL.md`:

```
skills/
  reproducing-bugs/
    SKILL.md        # frontmatter `name` and `description`, then instructions
    helpers.py      # optional files the instructions refer to
```

The built-in runner lists every skill's name and description to the agent and loads the
instructions only when the agent asks for them. Add more locations (for example a project's
`.claude/skills`) under `[skills] paths` in `triage.toml`, or with `ai-triager init --skills`.
`./triage.py skills` lists what the agents can see.
