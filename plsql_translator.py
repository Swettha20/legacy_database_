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

from llm_provider import ask_llm, provider_name


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


def is_structurally_valid_plpgsql(code: str) -> tuple[bool, str]:
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
    raw_output = ask_llm(build_prompt(plsql_code, retry=False))
    cleaned = clean_code_output(raw_output)

    is_valid, error = is_structurally_valid_plpgsql(cleaned)
    if is_valid:
        print("  [translate] Attempt 1 is structurally valid.")
        return cleaned

    print(f"  [translate] Attempt 1 invalid ({error}), retrying...")
    raw_retry = ask_llm(build_prompt(plsql_code, retry=True, error=error))
    cleaned_retry = clean_code_output(raw_retry)

    is_valid_retry, error_retry = is_structurally_valid_plpgsql(cleaned_retry)
    if is_valid_retry:
        print("  [translate] Retry is structurally valid.")
        return cleaned_retry

    print("  [translate] Still invalid after retry, returning with warning.")
    warning = (
        "-- WARNING: This PL/pgSQL failed structural validation even after a\n"
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

    result = translate_procedure(plsql_code)

    print("\n--- Translated PL/pgSQL ---")
    print(result)

    with open("checkout_book.plpgsql.sql", "w") as f:
        f.write(result)
    print("\nSaved to checkout_book.plpgsql.sql")
    