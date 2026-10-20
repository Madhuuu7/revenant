"""Model calls, and why they are observations rather than effects.

A model call is a *read*: calling it twice with the same prompt costs money
and returns possibly different text, but it does not change the world. So it
is journaled as an observation -- recorded on the first execution, read back
on replay -- and if a crash lands between ``ModelCalled`` and
``ModelReturned``, the call is simply retried. That retry can be a duplicate
charge; it cannot be a duplicate side effect. Treating it as an effect instead
would mean asking the provider to dedupe a key it does not accept, which buys
nothing.

Two implementations:

``ScriptedModel``
    A deterministic pseudo-model: text is derived from a hash of the prompt
    and a seed. It exists so the whole system -- API, dashboard, fault sweep,
    deployed demo -- works with no API key and no network, and so the sweep's
    thousands of runs are reproducible. It is not pretending to be an LLM; it
    is the stand-in a replay test wants, because replay is being tested, not
    the model.

``HttpModel``
    An OpenAI-shaped client, which covers Groq, OpenRouter, Together and
    llama.cpp's server as well as OpenAI itself. Reaches the network, so it is
    off by default and has never been run against a paid endpoint here -- see
    "Known limits" in the README. Determinism then comes from the journal, not
    from the provider: a replay never calls it again.
"""

from __future__ import annotations

import abc
import hashlib
import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelResult:
    text: str
    input_tokens: int
    output_tokens: int


class Model(abc.ABC):
    name: str = "model"

    @abc.abstractmethod
    def complete(self, prompt: str) -> ModelResult: ...


_VERBS = ("summarise", "compare", "extract", "verify", "draft", "rank")
_NOUNS = ("findings", "sources", "metrics", "claims", "notes", "signals")


class ScriptedModel(Model):
    """Deterministic in (seed, prompt). Same prompt, same text, always."""

    def __init__(self, seed: int = 7, name: str = "scripted-1"):
        self.seed = seed
        self.name = name
        self.calls = 0

    def complete(self, prompt: str) -> ModelResult:
        self.calls += 1
        h = hashlib.blake2b(
            f"{self.seed}:{prompt}".encode("utf-8"), digest_size=16
        ).digest()
        verb = _VERBS[h[0] % len(_VERBS)]
        noun = _NOUNS[h[1] % len(_NOUNS)]
        # A deterministic-but-prompt-dependent length, so timeline rows differ
        # from each other the way real completions do.
        detail = h[2:6].hex()
        text = f"{verb} {noun}: {detail}"
        return ModelResult(
            text=text,
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(text) // 4),
        )


class HttpModel(Model):
    """OpenAI-compatible chat completions over HTTP."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ):
        self.name = model or os.environ.get("REVENANT_MODEL", "gpt-4o-mini")
        self.base_url = (
            base_url
            or os.environ.get("REVENANT_MODEL_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("REVENANT_MODEL_API_KEY", "")
        self.timeout = timeout

    def complete(self, prompt: str) -> ModelResult:
        import httpx

        if not self.api_key:
            raise RuntimeError("REVENANT_MODEL_API_KEY is not set")
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.name,
                "messages": [{"role": "user", "content": prompt}],
                # Not a determinism mechanism -- the journal is. This only
                # keeps the first execution's text tighter.
                "temperature": 0,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        usage = body.get("usage") or {}
        return ModelResult(
            text=body["choices"][0]["message"]["content"],
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )


def default_model() -> Model:
    """Scripted unless a key is present, so the demo always works."""
    if os.environ.get("REVENANT_MODEL_API_KEY"):
        return HttpModel()
    return ScriptedModel(seed=int(os.environ.get("REVENANT_SEED", "7")))
