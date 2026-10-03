"""
legacy-db-modernizer: Day 11 (updated: config.py, then: AI-down resilience)
For columns the rule-based type_mapper.py couldn't confidently resolve,
ask the local AI model for a more informed suggestion - validated before
being trusted.

UPDATE (AI-down resilience): calling Ollama used to have zero error
handling - if Ollama was unreachable, this crashed with a raw Python
traceback all the way up through type_mapper.py, instead of failing
cleanly. Fixed with: a short timeout per attempt (so a dead service fails
fast, not hangs), a small number of retries with a brief wait between
them (so a momentary blip doesn't fail the whole run), and if all
retries are exhausted, a clean fallback to the safe default (NUMERIC)
with an honest note explaining the AI was unreachable - instead of
crashing the entire migration over one unavailable advisory call.
"""

import time
import requests
from config import OLLAMA_URL, OLLAMA_MODEL

VALID_SUGGESTIONS = {"INTEGER", "BIGINT", "NUMERIC", "REAL", "DOUBLE PRECISION"}

REQUEST_TIMEOUT_SECONDS = 15
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 3


def ask_ollama(prompt: str) -> str:
    """
    Raises requests.exceptions.RequestException (connection errors,
    timeouts, bad status codes) if Ollama is unreachable - callers are
    responsible for catching this, since what to do on failure (retry,
    fall back, give up) is a decision specific to each call site.
    """
    response = requests.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()["response"]


def ask_ollama_with_retry(prompt: str) -> str | None:
    """
    Wraps ask_ollama with a small number of retries and a short delay
    between attempts. Returns None (instead of raising) if every attempt
    fails, so callers can fall back to a safe default rather than crash.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return ask_ollama(prompt)
        except requests.exceptions.RequestException as e:
            print(f"  [ai_type_advisor] Ollama call failed (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    return None


def suggest_type_for_ambiguous_column(table_name: str, column_name: str) -> dict:
    prompt = (
        f"You are reviewing a database column during an Oracle to PostgreSQL "
        f"migration. The column '{column_name}' in table '{table_name}' has "
        f"Oracle type NUMBER with no precision or scale specified.\n\n"
        f"Based on the column name, suggest the single most likely PostgreSQL "
        f"type from this exact list: INTEGER, BIGINT, NUMERIC, REAL, DOUBLE PRECISION.\n\n"
        f"Reply with ONLY the type name, nothing else - no explanation, "
        f"no punctuation, just one of the exact words from the list above."
    )

    raw_response = ask_ollama_with_retry(prompt)

    if raw_response is None:
        return {
            "pg_type": "NUMERIC",
            "confidence": "needs_review",
            "note": f"AI advisor unreachable after {MAX_RETRIES} attempts - "
                    f"fell back to safe default NUMERIC. Please confirm manually "
                    f"and check that Ollama is running.",
        }

    suggestion = raw_response.strip().upper()

    if suggestion in VALID_SUGGESTIONS:
        return {
            "pg_type": suggestion,
            "confidence": "needs_review",
            "note": f"AI-suggested type based on column name '{column_name}' "
                    f"(originally ambiguous bare NUMBER) - please confirm",
        }

    return {
        "pg_type": "NUMERIC",
        "confidence": "needs_review",
        "note": f"AI suggestion ('{suggestion}') was not a recognized type - "
                f"fell back to safe default NUMERIC. Please confirm manually.",
    }


if __name__ == "__main__":
    test_cases = [
        ("books", "total_copies"),
        ("books", "available_copies"),
    ]

    for table, column in test_cases:
        result = suggest_type_for_ambiguous_column(table, column)
        print(f"{table}.{column}:")
        print(f"  -> {result['pg_type']} ({result['confidence']})")
        print(f"  {result['note']}\n")
