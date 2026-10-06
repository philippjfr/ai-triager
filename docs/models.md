# Models and keys

ai-triager uses three kinds of models, each configurable per task.

| Task | Kind | Setting |
|---|---|---|
| Triage and review agents | LLM with tool use | `triage_model`, `review_model` |
| Chat | LLM | `chat_model` |
| Label review, PR review, execution guard | Decision model (or an LLM as fallback for labels) | `decision_model`, `[prs] model`, `[sandbox.guard] model` |
| PR comment drafts | LLM | `[prs] explain_model` |

## LLMs

The built-in runner is built on [pydantic-ai](https://ai.pydantic.dev), so models are `provider:model` strings and any provider works with an API key: `openai:gpt-6-luna`, `anthropic:claude-sonnet-5-5`, `google-gla:gemini-3-pro`, `openrouter:openai/gpt-6-luna`, `groq:...` and so on. List the choices offered in the app under `[agents] models`.

Self-hosted or OpenAI-compatible servers are configured as named endpoints:

```toml
[agents.endpoints.local]
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_API_KEY"   # optional
```

and then used as `local:my-model`.

## Decision models

A decision model answers closed questions about a piece of state with calibrated probabilities instead of free text. ai-triager uses them wherever a yes/no judgement is needed at scale: does this label apply, does this PR follow the template, is this command safe to run.

| Model string | Provider | Key |
|---|---|---|
| `jev:jev-latest` | [TypeSafe](https://typesafe.ai) Jev via `typesafe-sdk` | `TYPESAFE_API_KEY` (or `JEV_API_KEY`) |
| `openrouter-decisions:~typesafe/jev-latest` | Jev through OpenRouter's decisions router | `OPENROUTER_API_KEY` |

## API keys

Keys are looked up per provider in this order: the environment variable, then the key stored with `./triage.py keys --set VAR` or in the app (in `~/.config/ai-triager/credentials.json`, mode 600), then a file you point to in the setup guide. They never live in the workspace.

Keys are only given to the model clients in the harness process. Commands that agents run do not inherit them: the agent's shell gets a minimal environment, and the sandbox blocks reading the credential files.

```bash
./triage.py keys                         # which providers have a key, and where it comes from
./triage.py keys --set OPENAI_API_KEY
```
