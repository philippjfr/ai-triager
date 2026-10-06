"""Decision models: calibrated answers to closed questions, as opposed to generated text.

Mirrors lumen's ``DecisionModel`` interface. A call sends one shared *state* and named questions;
the answers come back typed:

- ``noul``: probability (0-1) that the answer is yes;
- ``choice``: one of the given options with per-option probabilities;
- ``score``: an ordinal score with probabilities.

Providers, used through model strings like any other model:

- ``jev:jev-latest``: TypeSafe's Jev via ``typesafe-sdk`` (key ``TYPESAFE_API_KEY`` or ``JEV_API_KEY``);
- ``openrouter-decisions:~typesafe/jev-latest``: the same model through OpenRouter's Decisions router.
"""
from __future__ import annotations

from typing import Any

from . import credentials
from .config import Config

PROVIDERS = {
    'jev': {'label': 'Jev (TypeSafe)', 'default_model': 'jev-latest'},
    'openrouter-decisions': {'label': 'Jev via OpenRouter', 'default_model': '~typesafe/jev-latest',
                             'base_url': 'https://openrouter.ai/api'},
}


def is_decision_model(model: str) -> bool:
    return model.partition(':')[0] in PROVIDERS


def noul(instructions: str, yes: str | None = None, no: str | None = None) -> dict:
    criteria = {k: v for k, v in (('true', yes), ('false', no)) if v}
    return {'type': 'noul', 'instructions': instructions, **({'criteria': criteria} if criteria else {})}


async def invoke(model: str, state: Any, questions: dict[str, dict], cfg: Config | None = None,
                 timeout: float = 120) -> dict:
    """Answer all questions against one state; returns ``{'model', 'answers', 'usage'}``."""
    provider, _, name = model.partition(':')
    if provider not in PROVIDERS:
        raise ValueError(f'{model} is not a decision model; use one of {", ".join(PROVIDERS)}')
    if not questions:
        raise ValueError('At least one decision question is required.')
    key, _ = credentials.resolve(provider, cfg)
    if not key:
        raise ValueError(f'No API key for {PROVIDERS[provider]["label"]}.')
    name = name or PROVIDERS[provider]['default_model']
    if provider == 'jev':
        try:
            from typesafe_sdk import AsyncTypeSafeClient
        except ImportError as e:
            raise ImportError('Install typesafe-sdk (`pip install ai-triager[decisions]`) to use Jev.') from e
        client = AsyncTypeSafeClient(api_key=key, timeout=timeout)
        try:
            response = await client.system_one(state, questions, model=name)
        finally:
            await client.aclose()
        return response.model_dump()
    import httpx

    async with httpx.AsyncClient(base_url=PROVIDERS[provider]['base_url'], timeout=timeout,
                                 headers={'Authorization': f'Bearer {key}'}) as client:
        response = await client.post('/alpha/decisions', json={'model': name, 'state': state,
                                                               'questions': questions})
        response.raise_for_status()
        return response.json()
