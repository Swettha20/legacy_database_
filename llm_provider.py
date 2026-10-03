"""
legacy-db-modernizer: shared AI provider layer

One place that knows how to talk to an AI model. Every script that needs
AI (type advice, PL/SQL translation, the plugin system under app/) calls
ask_llm() instead of talking to Ollama directly, so switching provider is
a .env change, not a code change.

Providers:
  ollama (default) - local model, no key, no internet. Request format is
                     identical to what the project always sent.
  groq             - Groq cloud API (OpenAI-compatible), free tier, needs
                     GROQ_API_KEY in .env.

Why the default is "ollama": if LLM_PROVIDER is missing from .env, behavior
is exactly what it was before this module existed. Switching to Groq is
opt-in, and switching back is one line.

Why this reads env vars itself instead of importing config.py: config.py
refuses to load without Oracle/Postgres passwords, but the plugin system
under app/ (Java/PHP/SQL converters) has no database and shouldn't need
one just to call an AI model. Same .env file, same variable names.

Contract: ask_llm() makes ONE attempt and raises
requests.exceptions.RequestException on any network/HTTP/format failure,
exactly like the old ask_ollama() did, so existing callers' retry and
fallback logic keeps working unchanged. A missing API key raises
LLMConfigError (not a RequestException) on purpose: a misconfiguration
should fail loudly, not be silently retried or papered over by a fallback.
"""

import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

# Per-provider default timeouts. Local CPU inference of a whole stored
# procedure can legitimately take minutes (the translator originally had
# NO timeout at all), so Ollama gets a generous limit; a cloud API that
# hasn't answered in a minute is genuinely stuck. Override both with
# LLM_TIMEOUT_SECONDS in .env.
DEFAULT_TIMEOUT_SECONDS = {"ollama": 600, "groq": 60}
MAX_RATE_LIMIT_WAIT_SECONDS = 20


def _raise_for_status_with_body(response: requests.Response) -> None:
    """Like raise_for_status(), but keeps the provider's own explanation.

    A bare "404 Not Found" cost real debugging time when Groq retired a
    model; the response body says exactly what is wrong (model not found,
    bad key, rate limit...), so include it.
    """
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        detail = (response.text or "").strip()[:300]
        message = f"{e} | provider said: {detail}" if detail else str(e)
        raise requests.exceptions.HTTPError(message, response=response) from e


class LLMConfigError(RuntimeError):
    """The selected provider is not configured correctly (e.g. missing key)."""


def _settings() -> dict:
    # Read at call time, not import time, so tests and .env edits take
    # effect without surprises.
    return {
        "provider": os.getenv("LLM_PROVIDER", "ollama").strip().lower(),
        "ollama_url": os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate"),
        "ollama_model": os.getenv("OLLAMA_MODEL", "codellama:7b"),
        "groq_url": os.getenv("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions"),
        "groq_model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
        "groq_key": os.getenv("GROQ_API_KEY"),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0.2")),
        "timeout": os.getenv("LLM_TIMEOUT_SECONDS"),
    }


def provider_name() -> str:
    """Human-readable provider + model, for log lines and error notes."""
    s = _settings()
    if s["provider"] == "groq":
        return f"Groq ({s['groq_model']})"
    return f"Ollama ({s['ollama_model']})"


def _ask_ollama(s: dict, prompt: str, timeout: float) -> str:
    response = requests.post(
        s["ollama_url"],
        json={"model": s["ollama_model"], "prompt": prompt, "stream": False},
        timeout=timeout,
    )
    _raise_for_status_with_body(response)
    try:
        return response.json()["response"]
    except (KeyError, TypeError) as e:
        raise requests.exceptions.RequestException(
            f"Unexpected response shape from Ollama: {e}"
        ) from e


def _retry_after_seconds(response: requests.Response):
    try:
        return float(response.headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


def _ask_groq(s: dict, prompt: str, timeout: float) -> str:
    if not s["groq_key"]:
        raise LLMConfigError(
            "LLM_PROVIDER=groq but GROQ_API_KEY is not set - add it to your .env file "
            "(or set LLM_PROVIDER=ollama to use the local model)."
        )

    headers = {"Authorization": f"Bearer {s['groq_key']}"}
    body = {
        "model": s["groq_model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": s["temperature"],
    }

    # Free tiers rate-limit (HTTP 429). Ollama never did, so callers have
    # no handling for it. If the server says "retry in a few seconds", wait
    # that long once here; anything longer is raised for the caller's own
    # retry/fallback logic instead of freezing the pipeline.
    for attempt in (1, 2):
        response = requests.post(s["groq_url"], headers=headers, json=body, timeout=timeout)
        if response.status_code == 429 and attempt == 1:
            wait = _retry_after_seconds(response)
            if wait is not None and wait <= MAX_RATE_LIMIT_WAIT_SECONDS:
                print(f"  [llm] Groq rate limit hit - waiting {wait:.0f}s then retrying once...")
                time.sleep(wait)
                continue
        _raise_for_status_with_body(response)
        break

    try:
        return response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise requests.exceptions.RequestException(
            f"Unexpected response shape from Groq: {e}"
        ) from e


def ask_llm(prompt: str, timeout: float = None) -> str:
    """Send a prompt to the configured provider; return the text reply."""
    s = _settings()
    if timeout is None:
        if s["timeout"]:
            timeout = float(s["timeout"])
        else:
            timeout = DEFAULT_TIMEOUT_SECONDS.get(s["provider"], 60)

    if s["provider"] == "groq":
        return _ask_groq(s, prompt, timeout)
    if s["provider"] == "ollama":
        return _ask_ollama(s, prompt, timeout)
    raise LLMConfigError(
        f"Unknown LLM_PROVIDER '{s['provider']}' - use 'ollama' or 'groq'."
    )


def list_groq_models() -> list:
    """IDs of models your Groq key can currently use (Groq retires models
    regularly - when a call returns 404/model_not_found, pick one of these
    and set GROQ_MODEL in .env)."""
    s = _settings()
    if not s["groq_key"]:
        raise LLMConfigError("GROQ_API_KEY is not set - add it to your .env file.")
    url = s["groq_url"].replace("/chat/completions", "/models")
    response = requests.get(url, headers={"Authorization": f"Bearer {s['groq_key']}"}, timeout=30)
    _raise_for_status_with_body(response)
    return sorted(m["id"] for m in response.json()["data"])


if __name__ == "__main__":
    if "--models" in sys.argv:
        print("Models available to your Groq key:")
        for model_id in list_groq_models():
            print(f"  {model_id}")
        sys.exit(0)
    print(f"Provider: {provider_name()}")
    print(ask_llm("Reply with exactly one word: pong"))
