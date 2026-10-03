"""
legacy-db-modernizer: Day 12 (generalised: any procedure or function)
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

Generalised version: translates EVERY stored procedure and function in the
Oracle schema (or in a script file), not one hardcoded sample. Each result is
checked three ways before it is trusted - an Oracle-construct scan
(plsql_rules.py, every replacement verified on a real PostgreSQL), a real
PostgreSQL parse of the code (CREATE FUNCTION inside a transaction that is
rolled back), and, if either finds a problem, the exact error is fed back to
the AI for another attempt. What cannot be translated automatically is
reported for a person instead of guessed, and behaviours that translate fine
but differ between the databases are listed as review notes.

    python plsql_translator.py                       # all procedures/functions in the Oracle schema
    python plsql_translator.py --name CHECKOUT_BOOK  # just one
    python plsql_translator.py --file my_script.sql  # from a script instead of Oracle
    python plsql_translator.py --install             # also create the clean ones in Postgres
"""

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from llm_provider import ask_llm, provider_name, LLMConfigError
from plsql_rules import (
    code_only, find_fix_problems, find_manual_constructs, find_advisories,
    select_into_without_strict,
)

AI_MAX_ATTEMPTS = 2                 # per request, for transient network failures
AI_RETRY_DELAY_SECONDS = 3
MAX_TRANSLATION_ATTEMPTS = 3        # re-asks when the result is invalid


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


# ------------------------------------------------------------- validation


def is_structurally_valid_plpgsql(code: str, original_plsql: str = "") -> tuple[bool, str]:
    """
    Structure checks, an Oracle-construct scan, and (when the original
    source is given) checks that compare against it. Returns (ok, problems),
    where problems lists EVERYTHING found so the AI can fix it in one go.
    Callers that pass no original keep working: the comparisons are skipped.
    """
    code_upper = code.upper()

    if "CREATE OR REPLACE FUNCTION" not in code_upper and "CREATE FUNCTION" not in code_upper:
        return False, "Missing CREATE FUNCTION - Postgres uses FUNCTION, not PROCEDURE, for this pattern"

    if "$$" not in code:
        return False, "Missing $$ body delimiters, required by Postgres PL/pgSQL functions"

    if "LANGUAGE PLPGSQL" not in code_upper and "LANGUAGE 'PLPGSQL'" not in code_upper:
        return False, "Missing LANGUAGE plpgsql declaration at the end of the function"

    begin_count = len(re.findall(r"\bBEGIN\b", code_upper))
    end_count = len(re.findall(r"\bEND\b", code_upper))
    if begin_count == 0:
        return False, "No BEGIN block found"
    if end_count < begin_count:
        return False, f"Mismatched BEGIN/END ({begin_count} BEGIN, {end_count} END)"

    if re.search(r"RAISE EXCEPTION\s+'-\d+", code_upper):
        return False, ("RAISE EXCEPTION contains a malformed Oracle-style error code "
                       "inside the string - should be RAISE EXCEPTION 'message', "
                       "with the message properly formatted using %, not Oracle's || concatenation")

    problems = find_fix_problems(code)
    original_upper = original_plsql.upper()
    body = code_only(code)

    if original_plsql:
        # CURRENT_DATE silently drops the time of day (stores midnight), while
        # Oracle's SYSDATE includes the time. Matches the type mapper, which maps
        # Oracle DATE to TIMESTAMP for exactly this reason.
        if "SYSDATE" in original_upper and re.search(r"\bCURRENT_DATE\b", body):
            problems.append("CURRENT_DATE drops the time of day, but Oracle's SYSDATE includes "
                            "it - use NOW() (e.g. NOW() + INTERVAL '14 days')")

        # Verified on Postgres: a plain SELECT INTO returns NULL on zero rows and
        # silently takes one row of several, where Oracle always raises. STRICT
        # restores Oracle's behaviour (and makes WHEN NO_DATA_FOUND work at all).
        if select_into_without_strict(code):
            problems.append("a plain SELECT ... INTO does not raise NO_DATA_FOUND / TOO_MANY_ROWS "
                            "the way Oracle does (Postgres silently returns NULL or the first row) - "
                            "write every SELECT ... INTO as SELECT ... INTO STRICT variable")

    if problems:
        return False, "; ".join(problems)
    return True, ""


_FUNCTION_NAME = re.compile(
    r'CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?:"?[\w$]+"?\s*\.\s*)?("?)([\w$]+)\1\s*\(', re.IGNORECASE)


def _function_name(sql: str):
    m = _FUNCTION_NAME.search(sql)
    if not m:
        return None
    return m.group(2) if m.group(1) else m.group(2).lower()


def _drop_existing_versions(cursor, sql: str) -> list:
    """
    Drops any earlier version of the function being created, by NAME.

    CREATE OR REPLACE FUNCTION refuses to change a function's return type, so
    a translation that returns a different type than the copy installed by an
    earlier run was rejected ("cannot change return type of existing
    function") even though the new code was fine - wasting AI attempts and
    letting the OLD deployed signature steer the new translation. An Oracle
    procedure/function name is unique within its schema, so there is exactly
    one Postgres function per name and replacing by name is right. Only
    ordinary functions in the current schema are touched, never ones that
    belong to an extension. Run it inside a transaction: it is undone if the
    new function then fails to create.
    """
    name = _function_name(sql)
    if not name:
        return []
    cursor.execute(
        """
        SELECT p.oid::regprocedure::text
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = current_schema() AND p.proname = %s AND p.prokind = 'f'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
        """,
        (name,),
    )
    signatures = [row[0] for row in cursor.fetchall()]
    for signature in signatures:
        cursor.execute(f"DROP FUNCTION {signature}")
    return signatures


# What Postgres says is accurate but did not always get the AI to fix it (it
# ignored "result type must be integer because of OUT parameters" twice in a
# real run). Known messages get a one-line instruction appended.
_POSTGRES_HINTS = [
    (r"because of OUT parameters",
     "A function with OUT parameters must NOT declare RETURNS void (or any other type): leave the RETURNS clause out entirely."),
    (r"loop variable of loop over rows must be a record variable",
     "A FOR loop over query rows needs a RECORD variable (declare r RECORD;) or a list of scalar variables matching the columns."),
    (r"end label",
     "Close the function body with a plain END; - no name after END."),
    (r"unrecognized exception condition",
     "Use Postgres condition names: unique_violation, division_by_zero, no_data_found, too_many_rows, invalid_text_representation, others."),
    (r"type \"?\w+\"? does not exist",
     "Use Postgres types only: INTEGER, NUMERIC, VARCHAR, TEXT, TIMESTAMP, BOOLEAN."),
]


def _with_hint(message: str) -> str:
    for pattern, hint in _POSTGRES_HINTS:
        if re.search(pattern, message, re.IGNORECASE):
            return f"{message}. {hint}"
    return message


def check_in_postgres(sql: str):
    """
    Asks a real PostgreSQL to parse the function: CREATE FUNCTION runs inside a
    transaction that is always rolled back, so nothing is created. This catches
    what regexes cannot (syntax errors, unknown exception names, bad types,
    wrong END labels - all verified). Returns (True, "") if it parses,
    (False, reason) if Postgres rejects it, and (None, reason) if it could not
    be checked at all (Postgres not running / not configured).
    """
    try:
        import psycopg2
        from config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME
        conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD,
                                dbname=PG_DBNAME, connect_timeout=5)
    except Exception as e:
        return None, f"Postgres syntax check skipped ({str(e).splitlines()[0][:100]})"
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL check_function_bodies = on")
        cur.execute("SAVEPOINT before_drop")
        try:
            _drop_existing_versions(cur, sql)
        except psycopg2.Error:
            # Something depends on the old version (a view, say). Validate without dropping it.
            cur.execute("ROLLBACK TO SAVEPOINT before_drop")
        cur.execute(sql)          # no parameters: a literal % in RAISE is not a placeholder here
        return True, ""
    except psycopg2.Error as e:
        message = (e.diag.message_primary if e.diag and e.diag.message_primary else str(e)).strip()
        return False, f"Postgres rejected the function: {_with_hint(message)}"
    finally:
        conn.rollback()
        conn.close()


# ----------------------------------------------------------------- prompt


def build_prompt(plsql_code: str, retry: bool = False, error: str = "") -> str:
    kind = "function" if re.match(r"\s*(CREATE\s+(OR\s+REPLACE\s+)?)?(EDITIONABLE\s+)?FUNCTION\b", plsql_code, re.I) else "procedure"
    base = (
        f"Convert the following Oracle PL/SQL stored {kind} to a PostgreSQL "
        "PL/pgSQL function.\n\n"
        "REQUIRED, follow these exactly:\n\n"
        "1. Structure - use exactly this shape:\n"
        "   CREATE OR REPLACE FUNCTION function_name(params...)\n"
        "   RETURNS <type> AS $$\n"
        "   DECLARE\n"
        "       ...variable declarations...\n"
        "   BEGIN\n"
        "       ...body...\n"
        "   END;\n"
        "   $$ LANGUAGE plpgsql;\n"
        "   - An Oracle PROCEDURE without OUT parameters becomes RETURNS void.\n"
        "   - An Oracle FUNCTION becomes RETURNS followed by its converted RETURN type.\n"
        "   - OUT / IN OUT parameters become OUT / INOUT parameters in the parameter list.\n"
        "   - Close with a plain END; (never END function_name;).\n"
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
        "like SYSDATE + 14 (14 days) becomes NOW() + INTERVAL '14 days'. "
        "Adding a number to a date column or variable means DAYS in Oracle (due_date + p_days); "
        "Postgres rejects timestamp + integer, so write due_date + p_days * INTERVAL '1 day'. "
        "The difference of two dates is a number of days in Oracle; use (a::date - b::date). "
        "TRUNC(date) becomes date_trunc('day', date). ADD_MONTHS(d, n) becomes "
        "d + make_interval(months => n).\n\n"
        "6. SELECT INTO - ALWAYS write SELECT ... INTO STRICT variable ... . Oracle "
        "raises NO_DATA_FOUND when no row matches and TOO_MANY_ROWS when several do; "
        "a plain SELECT ... INTO in Postgres silently returns NULL or just the first "
        "row. STRICT restores Oracle's behavior and also makes a WHEN NO_DATA_FOUND "
        "handler work. (Not for INSERT INTO ... SELECT or cursor FETCH ... INTO.)\n\n"
        "7. Types - never leave Oracle type names (NUMBER, VARCHAR2, PLS_INTEGER) in the "
        "output. Use INTEGER for whole-number IDs, counts and parameters, NUMERIC for "
        "decimals, and VARCHAR for text. Declare function parameters as plain types "
        "(no IN keyword needed). %TYPE and %ROWTYPE work as they are.\n\n"
        "8. Oracle-only functions and syntax - replace them like this: NVL(a,b) -> "
        "COALESCE(a,b); NVL2(a,b,c) -> CASE WHEN a IS NOT NULL THEN b ELSE c END; "
        "DECODE(...) -> CASE; ROWNUM -> LIMIT; remove FROM DUAL; MINUS -> EXCEPT; "
        "seq.NEXTVAL -> nextval('seq'); DBMS_OUTPUT.PUT_LINE(x) -> RAISE NOTICE '%', x; "
        "EXECUTE IMMEDIATE -> EXECUTE; SQL%ROWCOUNT -> GET DIAGNOSTICS n = ROW_COUNT; "
        "INSTR(s, sub) -> strpos(s, sub); explicit cursors: CURSOR c IS SELECT ... -> "
        "c CURSOR FOR SELECT ...; exception names: DUP_VAL_ON_INDEX -> unique_violation, "
        "ZERO_DIVIDE -> division_by_zero, INVALID_NUMBER -> invalid_text_representation. "
        "NO_DATA_FOUND, TOO_MANY_ROWS and OTHERS are the same in both.\n\n"
        "9. If part of the code CANNOT be translated (PRAGMA, BULK COLLECT, FORALL, "
        "collection or record types, CONNECT BY, UTL_/DBMS_ packages), do not invent "
        "something: translate everything else and leave a comment at that point "
        "starting with -- MANUAL: describing what is missing.\n\n"
    )
    if retry:
        base += (
            f"IMPORTANT: Your previous attempt had this specific problem: {error}\n"
            "Fix ONLY this issue while keeping everything else that was already correct.\n\n"
        )
    base += (
        "Return ONLY the PL/pgSQL code, no explanation, no markdown fences.\n\n"
        f"Oracle PL/SQL {kind}:\n{plsql_code}"
    )
    return base


# ------------------------------------------------------------ translation


@dataclass
class TranslationResult:
    name: str
    kind: str
    sql: str                       # the code; starts with a WARNING header if it is not valid
    ok: bool                       # passed every check
    attempts: int
    problems: list = field(default_factory=list)       # why it is not ok
    manual: list = field(default_factory=list)         # parts a person must finish
    advisories: list = field(default_factory=list)     # valid, but behaviour may differ
    pg_checked: bool = False

    @property
    def needs_review(self) -> bool:
        return (not self.ok) or bool(self.manual) or bool(self.advisories) or not self.pg_checked


def _warning_header(problems: str, attempts: int, retry_failed_reason: str = "") -> str:
    if retry_failed_reason:
        return (
            f"-- WARNING: This PL/pgSQL failed validation ({problems}) and the retry could not run\n"
            f"-- because the AI service became unreachable ({retry_failed_reason}).\n"
            "-- It may be incomplete or malformed. Review manually, or run again.\n\n"
        )
    return (
        f"-- WARNING: This PL/pgSQL failed validation even after {attempts} attempts\n"
        f"-- ({problems}). It may be incomplete or malformed.\n"
        "-- Please review manually before running against a database.\n\n"
    )


def _validate(code: str, original: str, check_postgres: bool):
    """(ok, problem_text, pg_checked)"""
    ok, problem = is_structurally_valid_plpgsql(code, original)
    if not ok:
        return False, problem, False
    if not check_postgres:
        return True, "", False
    verdict, message = check_in_postgres(code)
    if verdict is False:
        return False, message, True
    return True, "", verdict is True


def translate_procedure_detailed(plsql_code: str, name: str = "", kind: str = "procedure",
                                 check_postgres: bool = True) -> TranslationResult:
    manual = find_manual_constructs(plsql_code)
    advisories = find_advisories(plsql_code)

    attempt, error, best = 0, "", None
    while attempt < MAX_TRANSLATION_ATTEMPTS:
        attempt += 1
        label = "attempt 1" if attempt == 1 else f"retry {attempt - 1}"
        print(f"  [translate] Calling {provider_name()} ({label})...")
        try:
            raw = _ask_with_retry(build_prompt(plsql_code, retry=attempt > 1, error=error))
        except TranslationUnavailable as e:
            if best is None:
                raise
            print(f"  [translate] Retry could not run: {e}")
            return TranslationResult(name, kind, _warning_header(error, attempt, provider_name()) + best,
                                     False, attempt - 1, [error], manual, advisories)
        cleaned = clean_code_output(raw)
        ok, problem, pg_checked = _validate(cleaned, plsql_code, check_postgres)
        if ok:
            print(f"  [translate] {'Attempt 1' if attempt == 1 else 'Retry'} is structurally valid"
                  f"{' and accepted by Postgres' if pg_checked else ''}.")
            return TranslationResult(name, kind, cleaned, True, attempt, [], manual, advisories, pg_checked)
        print(f"  [translate] Attempt {attempt} invalid ({problem})"
              f"{', retrying...' if attempt < MAX_TRANSLATION_ATTEMPTS else ''}")
        best, error = cleaned, problem

    print("  [translate] Still invalid after retry, returning with warning.")
    return TranslationResult(name, kind, _warning_header(error, attempt) + best, False, attempt,
                             [error], manual, advisories)


def translate_procedure(plsql_code: str) -> str:
    """Compatibility wrapper: just the code (with a WARNING header if invalid)."""
    return translate_procedure_detailed(plsql_code).sql


# ------------------------------------------------------- finding the code

_OBJECT = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:NON)?EDITIONABLE\s+)?(PROCEDURE|FUNCTION)\s+"
    r"(?:\"?\w+\"?\s*\.\s*)?\"?([\w$#]+)\"?.*?^\s*/\s*$",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)


def split_plsql_script(text: str) -> list:
    """Every CREATE PROCEDURE / FUNCTION in a SQL*Plus-style script (each ends
    with a line containing only a slash). Anything else in the file - tables,
    inserts - is ignored."""
    return [{"name": m.group(2).upper(), "type": m.group(1).upper(),
             "source": re.sub(r"\n\s*/\s*$", "", m.group(0).rstrip())}
            for m in _OBJECT.finditer(text)]


def _result_file_text(result: TranslationResult) -> str:
    lines = [f"-- Translated from Oracle {result.kind.upper()} {result.name} by legacy-db-modernizer",
             f"-- Status: {'OK' if result.ok else 'NEEDS FIXING - see WARNING below'}"
             + ("" if result.pg_checked else "  (not parsed by Postgres)")]
    for note in result.manual:
        lines.append(f"-- MANUAL WORK NEEDED: {note}")
    for note in result.advisories:
        lines.append(f"-- REVIEW: {note}")
    return "\n".join(lines) + "\n\n" + result.sql.strip() + "\n"


def install_in_postgres(sql: str):
    """
    Creates the function for real, replacing any earlier version of the same
    name. Drop and create happen in ONE transaction, so if the new function
    fails to create, the old one is still there. Returns (True, note) -
    note says what was replaced - or (False, reason).
    """
    import psycopg2
    from config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=PG_DBNAME)
    try:
        cur = conn.cursor()
        replaced = _drop_existing_versions(cur, sql)
        cur.execute(sql)
        conn.commit()
        return True, (f"replaced earlier version: {', '.join(replaced)}" if replaced else "")
    except psycopg2.Error as e:
        conn.rollback()
        return False, (e.diag.message_primary or str(e)).strip()
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Translate Oracle PL/SQL procedures and functions to PL/pgSQL.")
    parser.add_argument("--name", help="translate only this procedure/function")
    parser.add_argument("--file", help="read procedures from a SQL script instead of from Oracle")
    parser.add_argument("--out", default="translated_procedures", help="folder for the translated files")
    parser.add_argument("--install", action="store_true", help="also create the clean results in Postgres")
    parser.add_argument("--no-pg-check", action="store_true", help="skip asking Postgres to parse each result")
    args = parser.parse_args(argv)

    try:
        if args.file:
            objects = split_plsql_script(Path(args.file).read_text(encoding="utf-8"))
            origin = args.file
        else:
            from read_schema import read_source_objects, ORACLE_SCHEMA
            objects = read_source_objects()
            origin = f"Oracle schema {ORACLE_SCHEMA}"
    except Exception as e:
        print(f"ERROR: could not read the procedures - {e}")
        print("Tip: translate from a script instead with --file your_script.sql")
        return 1

    if args.name:
        objects = [o for o in objects if o["name"].upper() == args.name.upper()]
    if not objects:
        print(f"No stored procedures or functions found in {origin}"
              + (f" named {args.name}" if args.name else "") + ".")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for obj in objects:
        print(f"\n=== {obj['type']} {obj['name']} ===")
        try:
            result = translate_procedure_detailed(obj["source"], obj["name"], obj["type"].lower(),
                                                  check_postgres=not args.no_pg_check)
        except (TranslationUnavailable, LLMConfigError) as e:
            print(f"\nERROR: translation did not run - {e}")
            print(f"Files already written to {out_dir}/ were left as they are.")
            return 1
        path = out_dir / f"{obj['name'].lower()}.sql"
        path.write_text(_result_file_text(result), encoding="utf-8")
        results.append((result, path))

        if args.install and result.ok and result.pg_checked and not result.manual:
            installed, why = install_in_postgres(result.sql)
            print(f"  [install] {'created in Postgres' + (' (' + why + ')' if why else '') if installed else 'NOT created: ' + why}")

    print("\n" + "=" * 64)
    print(f"Translated {len(results)} from {origin}  ->  {out_dir}/")
    for result, path in results:
        status = "OK" if result.ok and not result.manual else ("NEEDS FIXING" if not result.ok else "NEEDS MANUAL WORK")
        print(f"  {result.name:28} {status:18} attempts={result.attempts}"
              f"{'  review notes=' + str(len(result.advisories)) if result.advisories else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
