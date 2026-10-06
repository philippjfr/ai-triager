"""API keys for model providers.

Each provider's key comes from one configured source (`[agents.keys.<provider>]`):

- ``env``: an environment variable, the provider's standard one unless ``env`` names another
  (e.g. reuse ``ANACONDA_OPENAI_API_KEY`` for ``openai``);
- ``stored``: ``~/.config/ai-triager/credentials.json`` (mode 0600), filled by pasting a key in the
  app or with ``ai-triager keys --set``; it lives outside any workspace so it never reaches git;
- ``file``: a file holding just the key, or ``NAME=value`` lines like a ``.env`` file.

Without configuration the standard variable, its aliases and the stored file are tried in turn.
"""
from __future__ import annotations

import json
import os

from pathlib import Path

PATH = Path(os.environ.get('AI_TRIAGER_CREDENTIALS', '~/.config/ai-triager/credentials.json')).expanduser()

# pydantic-ai provider prefix -> environment variable it reads the key from.
PROVIDERS = {
    'anthropic': 'ANTHROPIC_API_KEY',
    'openai': 'OPENAI_API_KEY',
    'google-gla': 'GOOGLE_API_KEY',
    'openrouter': 'OPENROUTER_API_KEY',
    'groq': 'GROQ_API_KEY',
    'mistral': 'MISTRAL_API_KEY',
    'deepseek': 'DEEPSEEK_API_KEY',
    'zai': 'ZAI_API_KEY',
    'moonshotai': 'MOONSHOTAI_API_KEY',
    'together': 'TOGETHER_API_KEY',
    'fireworks': 'FIREWORKS_API_KEY',
    'cerebras': 'CEREBRAS_API_KEY',
    'gateway': 'PYDANTIC_AI_GATEWAY_API_KEY',
    # Decision models (see decisions.py), not LLMs.
    'jev': 'TYPESAFE_API_KEY',
    'openrouter-decisions': 'OPENROUTER_API_KEY',
}
LABELS = {
    'anthropic': 'Anthropic', 'openai': 'OpenAI', 'google-gla': 'Google Gemini', 'openrouter': 'OpenRouter',
    'groq': 'Groq', 'mistral': 'Mistral', 'deepseek': 'DeepSeek', 'zai': 'Z.ai', 'moonshotai': 'Moonshot',
    'together': 'Together', 'fireworks': 'Fireworks', 'cerebras': 'Cerebras', 'gateway': 'Pydantic AI Gateway',
    'jev': 'Jev (TypeSafe)', 'openrouter-decisions': 'Jev via OpenRouter',
}
DECISION_PROVIDERS = ('jev', 'openrouter-decisions')
# Starting points for the model pickers; any model id the provider accepts works.
SUGGESTED_MODELS = {
    'anthropic': ['claude-haiku-4-5', 'claude-sonnet-5-5', 'claude-opus-5-5'],
    'openai': ['gpt-6-luna', 'gpt-6-sol', 'gpt-5-mini'],
    'google-gla': ['gemini-3-flash-preview', 'gemini-3-pro-preview'],
    'openrouter': ['openai/gpt-6-luna', 'z-ai/glm-5.3-flash', 'anthropic/claude-sonnet-5.5'],
    'groq': ['moonshotai/kimi-k2-instruct'],
    'mistral': ['mistral-medium-latest'],
    'deepseek': ['deepseek-chat'],
    'jev': ['jev-latest'],
    'openrouter-decisions': ['~typesafe/jev-latest'],
}
# Other variables a provider also accepts.
ALIASES = {'GOOGLE_API_KEY': ['GEMINI_API_KEY'], 'TYPESAFE_API_KEY': ['JEV_API_KEY']}
SOURCES = ('env', 'stored', 'file')


def provider_of(model: str) -> str:
    prefix = model.split(':', 1)[0] if ':' in model else ''
    return 'gateway' if prefix.startswith('gateway/') else prefix


def load() -> dict[str, str]:
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}


def save(keys: dict[str, str]):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({k: v for k, v in keys.items() if v}, f, indent=2)
    os.chmod(PATH, 0o600)


def set_key(env_var: str, value: str):
    keys = load()
    keys[env_var] = value
    save(keys)


def source_of(provider: str, cfg=None) -> dict:
    agents = cfg.agents if cfg else {}
    spec = dict((agents.get('keys') or {}).get(provider) or {})
    endpoint = (agents.get('endpoints') or {}).get(provider) or {}
    spec.setdefault('source', 'auto')
    spec.setdefault('env', PROVIDERS.get(provider) or endpoint.get('api_key_env', ''))
    return spec


def _read_file(path: str, env_var: str) -> str | None:
    try:
        text = Path(path).expanduser().read_text().strip()
    except OSError:
        return None
    if '=' not in text:
        return text or None
    for line in text.splitlines():
        name, sep, value = line.strip().removeprefix('export ').partition('=')
        if sep and name.strip() == env_var:
            return value.strip().strip('"\'') or None
    return None


def resolve(provider: str, cfg=None) -> tuple[str | None, str]:
    """The key for a provider and a description of where it came from."""
    spec = source_of(provider, cfg)
    standard = PROVIDERS.get(provider) or spec['env']
    source = spec['source']
    if source in ('env', 'auto'):
        for name in [spec['env'], standard, *ALIASES.get(standard, [])]:
            if name and os.environ.get(name):
                return os.environ[name], f'environment variable {name}'
    if source in ('stored', 'auto') and load().get(standard):
        return load()[standard], f'stored in {PATH}'
    if source == 'file' and spec.get('file'):
        value = _read_file(spec['file'], standard)
        if value:
            return value, f"file {spec['file']}"
    return None, ''


def available(provider: str, cfg=None) -> bool:
    return resolve(provider, cfg)[0] is not None


def status(cfg=None) -> dict[str, str]:
    """``env var -> 'env' | 'stored' | 'file' | ''`` for every known provider."""
    out = {}
    for provider, env_var in PROVIDERS.items():
        _, where = resolve(provider, cfg)
        out[env_var] = where.split(' ', 1)[0].replace('environment', 'env') if where else ''
    return out


def apply_to_environ(cfg=None):
    """Put each provider's key into the variable pydantic-ai reads, honouring the configured source."""
    for provider, env_var in PROVIDERS.items():
        value, _ = resolve(provider, cfg)
        if value:
            os.environ[env_var] = value
