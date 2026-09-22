"""
legacy-db-modernizer: Day 11
For columns the rule-based type_mapper.py couldn't confidently resolve
(bare Oracle NUMBER, flagged "needs_review"), ask the local AI model for
a more informed suggestion based on the column's name and table context -
then validate the suggestion before trusting it.

This does NOT replace the rule-based mapper - it only runs for columns
already flagged as ambiguous. Rules handle everything solvable by rules;
AI is reserved for genuine judgment calls, and every AI suggestion is
checked before being accepted (never trusted blindly).
"""

import requests
import json
import re

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "codellama:7b"

# The only types we'll accept from the AI - anything outside this list
# gets rejected, no matter how confidently the model suggests it. This
# keeps the AI's role bounded: it picks from known-good options, it
# doesn't get to invent new Postgres syntax.
VALID_SUGGESTIONS = {"INTEGER", "BIGINT", "NUMERIC", "REAL", "DOUBLE PRECISION"}


def ask_ollama(prompt: str) -> str:
    response = requests.post(
        OLLAMA_URL,
        json={"model": MODEL_NAME, "prompt": prompt, "stream": False},
    )
    response.raise_for_status()
    return response.json()["response"]


def suggest_type_for_ambiguous_column(table_name: str, column_name: str) -> dict:
    """
    Asks the AI to suggest a precise Postgres numeric type for a bare
    Oracle NUMBER column, using only the table and column name as context
    (the same information a human reviewer would have, looking at the
    schema without sample data).
    """
    prompt = (
        f"You are reviewing a database column during an Oracle to PostgreSQL "
        f"migration. The column '{column_name}' in table '{table_name}' has "
        f"Oracle type NUMBER with no precision or scale specified.\n\n"
        f"Based on the column name, suggest the single most likely PostgreSQL "
        f"type from this exact list: INTEGER, BIGINT, NUMERIC, REAL, DOUBLE PRECISION.\n\n"
        f"Reply with ONLY the type name, nothing else - no explanation, "
        f"no punctuation, just one of the exact words from the list above."
    )

    raw_response = ask_ollama(prompt)
    suggestion = raw_response.strip().upper()

    # Validate: is this actually one of the types we said it could pick?
    if suggestion in VALID_SUGGESTIONS:
        return {
            "pg_type": suggestion,
            "confidence": "needs_review",  # still human-reviewable, but now an informed guess
            "note": f"AI-suggested type based on column name '{column_name}' "
                    f"(originally ambiguous bare NUMBER) - please confirm",
        }

    # The AI said something outside our allowed list - don't trust it,
    # fall back to the safe original default instead of guessing further.
    return {
        "pg_type": "NUMERIC",
        "confidence": "needs_review",
        "note": f"AI suggestion ('{suggestion}') was not a recognized type - "
                f"fell back to safe default NUMERIC. Please confirm manually.",
    }


if __name__ == "__main__":
    # Quick manual test against the two ambiguous columns we already know
    # about from mapped_schema.json: books.total_copies, books.available_copies
    test_cases = [
        ("books", "total_copies"),
        ("books", "available_copies"),
    ]

    for table, column in test_cases:
        result = suggest_type_for_ambiguous_column(table, column)
        print(f"{table}.{column}:")
        print(f"  -> {result['pg_type']} ({result['confidence']})")
        print(f"  {result['note']}\n")