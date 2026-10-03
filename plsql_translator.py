"""
legacy-db-modernizer: Day 12
Translates Oracle PL/SQL stored procedures into PostgreSQL PL/pgSQL,
using the configured AI model (Ollama or Groq) - then VALIDATES the result structurally and
retries with specific feedback if it fails, same pattern as the PHP
plugin's SQL-injection check and the Java plugin's ast.parse() check
from the earlier ai-code-migrator project.

Why AI is actually needed here (unlike type mapping): PL/SQL is a full
programming language, not a small fixed set of types. Writing rules for
one specific procedure wouldn't generalize to any other procedure someone
migrates - this is a genuine case for AI reasoning about logic, not just
lookup-table translation.

History of fixes:
- First attempt got most of the translation right but missed the
  LANGUAGE plpgsql declaration and used malformed RAISE EXCEPTION syntax.
  Fixed with a more explicit prompt template and examples.
- Second attempt (this version): the translated function passed structural
  validation and CREATE FUNCTION succeeded, but calling it failed with
  "invalid transaction termination" at ROLLBACK. Root cause: Oracle
  procedures can freely COMMIT/ROLLBACK, but a plain PL/pgSQL FUNCTION
  cannot - it runs inside whatever transaction the caller already started
  and isn't allowed to control the transaction boundary itself. Fixed by
  instructing the AI to omit COMMIT/ROLLBACK entirely and rely on
  Postgres's behavior of automatically rolling back a function's changes
  when it raises an exception - which is actually the more idiomatic
  Postgres pattern anyway, not just a workaround. Also added a validator
  check that specifically catches COMMIT/ROLLBACK inside the function body,
  since "structurally valid" alone didn't catch this class of bug - it
  only surfaces when you actually try to call the function.
"""

import re
import sys
import time

import requests

from llm_provider import ask_llm, provider_name, LLMConfigError

AI_MAX_ATTEMPTS = 2
AI_RETRY_DELAY_SECONDS = 3


class TranslationUnavailable(RuntimeError):
    """The AI provider could not be reached (after retries)."""


def _ask_with_retry(prompt: str) -> str:
    """
    One transient failure (a network blip, a rate limit, a service still
    starting) shouldn't fail the whole translation, but a service that is
    really down must fail fast and clearly - not with a raw traceback.
    Same retry pattern as ai_type_advisor.py.
    """
    last_error = None
    for attempt in range(1, AI_MAX_ATTEMPTS + 1):
        try:
            return ask_llm(prompt)
        except requests.exceptions.RequestException as e:
            last_error = e
            print(f"  [translate] AI call to {provider_name()} failed "
                  f"(attempt {attempt}/{AI_MAX_ATTEMPTS}): {e}")
            if attempt < AI_MAX_ATTEMPTS:
                time.sleep(AI_RETRY_DELAY_SECONDS)
    raise TranslationUnavailable(
        f"{provider_name()} was unreachable after {AI_MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error


def clean_code_output(raw_output: str) -> str:
    text = raw_output.strip()

    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 2:
            code_block = parts[1]
            lines = code_block.split("\n", 1)
            if len(lines) == 2 and lines[0].strip().isalpha():
                code_block = lines[1]
            return code_block.strip()

    return text


def _code_only(code: str) -> str:
    """Upper-cased code with string literals and -- comments blanked out, so
    keyword checks don't trip on words inside messages like 'invalid number'."""
    no_comments = re.sub(r"--[^\n]*", "", code)
    return re.sub(r"'[^']*'", "''", no_comments).upper()


def is_structurally_valid_plpgsql(code: str, original_plsql: str = "") -> tuple[bool, str]:
    """
    Structural checks, plus a few semantic checks for Oracle-to-Postgres
    differences that compile fine but behave differently at runtime (each
    one was verified against a real Postgres). Semantic checks that compare
    against the source need original_plsql; without it they are skipped, so
    existing callers keep working.
    """
    code_upper = code.upper()

    if "CREATE OR REPLACE FUNCTION" not in code_upper and "CREATE FUNCTION" not in code_upper:
        return False, "Missing CREATE FUNCTION - Postgres uses FUNCTION, not PROCEDURE, for this pattern"

    if "$$" not in code:
        return False, "Missing $$ body delimiters, required by Postgres PL/pgSQL functions"

    if "LANGUAGE PLPGSQL" not in code_upper and "LANGUAGE 'PLPGSQL'" not in code_upper:
        return False, "Missing LANGUAGE plpgsql declaration at the end of the function"

    begin_count = len(re.findall(r'\bBEGIN\b', code_upper))
    end_count = len(re.findall(r'\bEND\b', code_upper))
    if begin_count == 0:
        return False, "No BEGIN block found"
    if end_count < begin_count:
        return False, f"Mismatched BEGIN/END ({begin_count} BEGIN, {end_count} END)"

    if re.search(r"RAISE EXCEPTION\s+'-\d+", code_upper):
        return False, ("RAISE EXCEPTION contains a malformed Oracle-style error code "
                        "inside the string - should be RAISE EXCEPTION 'message', "
                        "with the message properly formatted using %, not Oracle's || concatenation")

    # Catches the real bug found by actually calling the translated
    # function: a plain PL/pgSQL FUNCTION cannot COMMIT or ROLLBACK - it
    # runs inside the caller's transaction. This only surfaces at call
    # time otherwise, not at CREATE FUNCTION time, so it's worth catching
    # here structurally instead of relying on someone testing a live call.
    if re.search(r'\bCOMMIT\s*;', code_upper) or re.search(r'\bROLLBACK\s*;', code_upper):
        return False, ("Function body contains COMMIT or ROLLBACK - not allowed inside a "
                        "plain PL/pgSQL FUNCTION (only in a PROCEDURE called via CALL). "
                        "Remove them entirely; Postgres automatically rolls back a "
                        "function's changes when it raises an exception.")

    body = _code_only(code)
    original_upper = original_plsql.upper()

    # Oracle type names left behind (a 7B model did this: v_available NUMBER).
    if re.search(r"\b(NUMBER|VARCHAR2|NVARCHAR2)\b", body):
        return False, ("Oracle-only type name (NUMBER / VARCHAR2) left in the code - "
                        "use INTEGER, NUMERIC or VARCHAR instead")

    if re.search(r"\bSYSDATE\b", body):
        return False, "SYSDATE is Oracle-only - use NOW()"

    # CURRENT_DATE silently drops the time of day (stores midnight), while
    # Oracle's SYSDATE includes the time. Matches the type mapper, which maps
    # Oracle DATE to TIMESTAMP for exactly this reason.
    if "SYSDATE" in original_upper and re.search(r"\bCURRENT_DATE\b", body):
        return False, ("CURRENT_DATE drops the time of day, but Oracle's SYSDATE includes "
                        "it - use NOW() (e.g. NOW() + INTERVAL '14 days')")

    # In Postgres, plain SELECT ... INTO never raises NO_DATA_FOUND (the
    # variable just becomes NULL), so an Oracle NO_DATA_FOUND handler becomes
    # dead code and the error message changes. INTO STRICT restores it.
    if ("NO_DATA_FOUND" in original_upper and "NO_DATA_FOUND" in body
            and not re.search(r"\bINTO\s+STRICT\b", body)):
        return False, ("WHEN NO_DATA_FOUND never fires in Postgres unless the query uses "
                        "SELECT ... INTO STRICT variable - add STRICT")

    return True, ""


def build_prompt(plsql_code: str, retry: bool = False, error: str = "") -> str:
    base = (
        "Convert the following Oracle PL/SQL stored procedure to a PostgreSQL "
        "PL/pgSQL function.\n\n"
        "REQUIRED, follow these exactly:\n\n"
        "1. Structure - use exactly this shape:\n"
        "   CREATE OR REPLACE FUNCTION function_name(params...)\n"
        "   RETURNS void AS $$\n"
        "   DECLARE\n"
        "       ...variable declarations...\n"
        "   BEGIN\n"
        "       ...body...\n"
        "   END;\n"
        "   $$ LANGUAGE plpgsql;\n"
        "   The LANGUAGE plpgsql; line at the very end, after the closing $$, is "
        "MANDATORY - the function is invalid without it.\n\n"
        "2. Error handling - Oracle's RAISE_APPLICATION_ERROR(-20001, 'some message') "
        "must become RAISE EXCEPTION 'some message'; - do NOT put the numeric error "
        "code inside the string. For messages built with concatenation like "
        "'text ' || variable, use PL/pgSQL's format style instead: "
        "RAISE EXCEPTION 'text %', variable;\n\n"
        "3. Keep the row-locking behavior: Oracle's SELECT ... FOR UPDATE has a "
        "direct PL/pgSQL equivalent, use it the same way.\n\n"
        "4. Do NOT include COMMIT or ROLLBACK anywhere in the function body. A plain "
        "PL/pgSQL FUNCTION is not allowed to control transactions - it always runs "
        "inside whatever transaction the caller already started. Simply omit "
        "COMMIT entirely, and omit ROLLBACK from any exception handler too - "
        "Postgres automatically rolls back everything the function did as soon as "
        "it raises an exception, so an explicit ROLLBACK is both invalid and "
        "unnecessary. Just re-raise the exception with RAISE; if you need a "
        "generic 'catch everything and re-throw' handler.\n\n"
        "5. Dates and times - Oracle's SYSDATE includes the time of day. Replace it "
        "with NOW(), NEVER with CURRENT_DATE (which drops the time). Date arithmetic "
        "like SYSDATE + 14 (14 days) becomes NOW() + INTERVAL '14 days'.\n\n"
        "6. NO_DATA_FOUND - in Postgres a plain SELECT ... INTO does NOT raise "
        "NO_DATA_FOUND when no row matches, so an Oracle WHEN NO_DATA_FOUND handler "
        "would never run. If the procedure has such a handler, write the query as "
        "SELECT ... INTO STRICT variable ... so the handler works as in Oracle.\n\n"
        "7. Types - never leave Oracle type names (NUMBER, VARCHAR2) in the output. "
        "Use INTEGER for whole-number IDs, counts and parameters, NUMERIC for "
        "decimals, and VARCHAR for text. Declare function parameters as plain types "
        "(no IN keyword needed).\n\n"
    )
    if retry:
        base += (
            f"IMPORTANT: Your previous attempt had this specific problem: {error}\n"
            "Fix ONLY this issue while keeping everything else that was already correct.\n\n"
        )
    base += (
        "Return ONLY the PL/pgSQL code, no explanation, no markdown fences.\n\n"
        f"Oracle PL/SQL procedure:\n{plsql_code}"
    )
    return base


def translate_procedure(plsql_code: str) -> str:
    print(f"  [translate] Calling {provider_name()} (attempt 1)...")
    raw_output = _ask_with_retry(build_prompt(plsql_code, retry=False))
    cleaned = clean_code_output(raw_output)

    is_valid, error = is_structurally_valid_plpgsql(cleaned, plsql_code)
    if is_valid:
        print("  [translate] Attempt 1 is structurally valid.")
        return cleaned

    print(f"  [translate] Attempt 1 invalid ({error}), retrying...")
    try:
        raw_retry = _ask_with_retry(build_prompt(plsql_code, retry=True, error=error))
    except TranslationUnavailable as e:
        # The first attempt worked well enough to produce something; don't
        # throw it away just because the AI went down before the retry.
        print(f"  [translate] Retry could not run: {e}")
        return (
            "-- WARNING: This PL/pgSQL failed validation "
            f"({error}) and the retry could not run\n"
            f"-- because the AI service became unreachable ({provider_name()}).\n"
            "-- It may be incomplete or malformed. Review manually, or run again.\n\n"
        ) + cleaned
    cleaned_retry = clean_code_output(raw_retry)

    is_valid_retry, error_retry = is_structurally_valid_plpgsql(cleaned_retry, plsql_code)
    if is_valid_retry:
        print("  [translate] Retry is structurally valid.")
        return cleaned_retry

    print("  [translate] Still invalid after retry, returning with warning.")
    warning = (
        "-- WARNING: This PL/pgSQL failed validation even after a\n"
        f"-- retry ({error_retry}). It may be incomplete or malformed.\n"
        "-- Please review manually before running against a database.\n\n"
    )
    return warning + cleaned_retry


if __name__ == "__main__":
    with open("sample_schema.sql") as f:
        full_sql = f.read()

    match = re.search(
        r"CREATE OR REPLACE PROCEDURE checkout_book.*?^END checkout_book;\s*\n/",
        full_sql,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        raise ValueError("Could not find checkout_book procedure in sample_schema.sql")

    plsql_code = match.group(0)
    print("--- Original PL/SQL ---")
    print(plsql_code)

    try:
        result = translate_procedure(plsql_code)
    except (TranslationUnavailable, LLMConfigError) as e:
        # Exit cleanly WITHOUT touching checkout_book.plpgsql.sql, so an
        # earlier good translation is never overwritten by a failed run.
        print(f"\nERROR: translation did not run - {e}")
        print("Existing checkout_book.plpgsql.sql (if any) was left unchanged.")
        sys.exit(1)

    print("\n--- Translated PL/pgSQL ---")
    print(result)

    with open("checkout_book.plpgsql.sql", "w") as f:
        f.write(result)
    print("\nSaved to checkout_book.plpgsql.sql")
    