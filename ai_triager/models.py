"""Turning model strings into pydantic-ai models, including custom OpenAI-compatible endpoints.

Built-in providers use pydantic-ai's own ``provider:model`` strings. A custom endpoint is declared
once and then used like a provider::

    [agents.endpoints.jev]
    base_url = "https://jev.example.com/v1"
    api_key_env = "JEV_API_KEY"

    [agents]
    decision_model = "jev:decision-1"
"""
from __future__ import annotations

from . import credentials
from .config import Config


def endpoints(cfg: Config | None) -> dict[str, dict]:
    return dict((cfg.agents.get('endpoints') or {}) if cfg else {})


def resolve(model: str, cfg: Config | None):
    """A pydantic-ai model object for custom endpoints, or the string itself for built-in providers."""
    prefix, _, name = model.partition(':')
    endpoint = endpoints(cfg).get(prefix)
    if not endpoint:
        return model
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    key, _ = credentials.resolve(prefix, cfg)
    provider = OpenAIProvider(base_url=endpoint['base_url'], api_key=key or 'not-needed')
    return OpenAIChatModel(name, provider=provider)
