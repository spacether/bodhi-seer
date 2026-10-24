"""Small wrapper around the Claude API, with a local OpenAI-compatible fallback."""
from __future__ import annotations

import json
import os
import re
from typing import Any

import requests

DEFAULT_MODEL = "qwen2.5:3b"
_client = None


def _model_name() -> str:
    return os.environ.get("BODHI_SEER_MODEL", DEFAULT_MODEL)


def _provider() -> str:
    return os.environ.get("BODHI_SEER_LLM_PROVIDER", "ollama").strip().lower()


def _base_url() -> str:
    return os.environ.get("BODHI_SEER_BASE_URL", "").strip()


def _api_key() -> str | None:
    return os.environ.get("BODHI_SEER_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")


def _default_local_base_url() -> str:
    return "http://localhost:11434/v1"


def available() -> bool:
    provider = _provider()
    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True
    if provider in {"openai", "openaicompatible", "openai-compatible", "ollama", "local"}:
        return bool(_base_url() or provider in {"ollama", "local"})
    return False


def _local_base_url() -> str:
    provider = _provider()
    base_url = _base_url()
    if base_url:
        return base_url.rstrip("/")
    if provider in {"ollama", "local", "openai", "openai-compatible", "openaicompatible"}:
        return _default_local_base_url()
    raise ValueError(f"Unsupported local LLM provider: {provider!r}")


def _get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()
    return _client


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_as_text(item) for item in value)
    if isinstance(value, dict):
        if "text" in value:
            return _as_text(value["text"])
        if "content" in value:
            return _as_text(value["content"])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def complete(system: str, prompt: str, max_tokens: int = 2000) -> str:
    provider = _provider()
    if provider == "anthropic":
        msg = _get_client().messages.create(
            model=_model_name(),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()

    url = f"{_local_base_url()}/chat/completions"
    headers = {"Content-Type": "application/json"}
    api_key = _api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": _model_name(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    response = requests.post(url, headers=headers, json=payload, timeout=180)
    response.raise_for_status()
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise ValueError(f"Local LLM returned no choices: {data}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(_as_text(item) for item in content).strip()
    return _as_text(content).strip()


def complete_json(system: str, prompt: str, max_tokens: int = 4000):
    """Ask for JSON and parse it, retrying once if the reply isn't valid JSON."""
    system = system + "\n\nRespond with a single JSON value only: no prose, no code fences."
    last_err = None
    for _ in range(2):
        text = complete(system, prompt, max_tokens)
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            last_err = e
            prompt += "\n\nYour previous reply was not valid JSON. Reply with valid JSON only."
    raise ValueError(f"Model did not return valid JSON: {last_err}")
