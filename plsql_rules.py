"""
legacy-db-modernizer: PL/SQL -> PL/pgSQL translation rules

A catalog of the Oracle constructs that cannot be left in translated code,
each with the replacement that was verified to work on a real PostgreSQL 16,
plus detectors for them. Pure text analysis: no database, no AI, so it can be
tested hard.

Three kinds of finding, because they need different handling:

  FIX       Oracle-only syntax that is still in the translated code (NVL,
            SYSDATE, DUAL ...). The AI is told exactly what to change and asked
            again. Many of these are accepted by CREATE FUNCTION and only fail
            when the function is CALLED (verified), which is why a static check
            is needed on top of asking Postgres to parse the code.

  MANUAL    Constructs with no reasonable automatic translation (PRAGMA,
            BULK COLLECT, collections, CONNECT BY ...). Asking again will not
            help, so they are reported for a human instead of looped on.

  ADVISORY  Things that translate and run fine but can BEHAVE differently
            (NULL in ||, integer division ...). Reported, never blocking.
"""

import re


def code_only(code: str) -> str:
    """Upper-cased code with comments and string literals blanked out, so
    keywords inside messages ('invalid number') or comments never match."""
    text = re.sub(r"/\*.*?\*/", " ", code, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)
    return re.sub(r"'(?:[^']|'')*'", "''", text).upper()


# ---------------------------------------------------------------- FIX rules
# (pattern on code_only text, message with the verified replacement)

FIX_RULES = [
    (r"\bNVL\s*\(", "NVL() does not exist in Postgres - use COALESCE(a, b)"),
    (r"\bNVL2\s*\(", "NVL2() does not exist in Postgres - use CASE WHEN a IS NOT NULL THEN b ELSE c END"),
    (r"\bDECODE\s*\(", "DECODE() does not exist in Postgres - use a CASE expression"),
    (r"\b(SYSDATE|SYSTIMESTAMP)\b", "SYSDATE/SYSTIMESTAMP are Oracle-only - use NOW()"),
    (r"\bROWNUM\b", "ROWNUM does not exist in Postgres - use LIMIT n (or ROW_NUMBER() OVER ())"),
    (r"\bFROM\s+DUAL\b", "DUAL does not exist in Postgres - drop FROM DUAL (SELECT expr works without a FROM)"),
    (r"\bMINUS\b", "MINUS is Oracle-only - use EXCEPT"),
    (r"\.\s*(NEXTVAL|CURRVAL)\b", "seq.NEXTVAL / seq.CURRVAL are Oracle-only - use nextval('seq') / currval('seq')"),
    (r"\bRAISE_APPLICATION_ERROR\b", "RAISE_APPLICATION_ERROR does not exist - use RAISE EXCEPTION 'message %', value"),
    (r"\bDBMS_OUTPUT\b", "DBMS_OUTPUT does not exist - use RAISE NOTICE '%', value"),
    (r"\bEXECUTE\s+IMMEDIATE\b", "EXECUTE IMMEDIATE is Oracle-only - use EXECUTE (with INTO ... USING ...)"),
    (r"\bSQL\s*%\s*(ROWCOUNT|NOTFOUND|FOUND)\b", "SQL%ROWCOUNT etc. are Oracle-only - use GET DIAGNOSTICS n = ROW_COUNT, or the FOUND variable"),
    (r"\bINSTR\s*\(", "INSTR() does not exist in Postgres - use strpos(string, substring)"),
    (r"\bADD_MONTHS\s*\(", "ADD_MONTHS() does not exist - use date + make_interval(months => n)"),
    (r"\bSQLCODE\b", "SQLCODE does not exist in Postgres - use SQLSTATE (text) or SQLERRM"),
    (r"\b(NUMBER|VARCHAR2|NVARCHAR2|PLS_INTEGER|BINARY_INTEGER)\b",
     "Oracle-only type name (NUMBER / VARCHAR2 / PLS_INTEGER ...) left in the code - use INTEGER, NUMERIC or VARCHAR instead"),
    (r"\bDUP_VAL_ON_INDEX\b", "DUP_VAL_ON_INDEX does not exist - the Postgres name is unique_violation"),
    (r"\bZERO_DIVIDE\b", "ZERO_DIVIDE does not exist - the Postgres name is division_by_zero"),
    (r"\b(INVALID_NUMBER|VALUE_ERROR)\b",
     "INVALID_NUMBER / VALUE_ERROR do not exist - use invalid_text_representation or numeric_value_out_of_range"),
    (r"\bCURSOR\s+\w+\s+IS\b", "CURSOR name IS select is Oracle syntax - declare it as: name CURSOR FOR select"),
    (r"\(\s*\+\s*\)", "(+) outer-join syntax is Oracle-only - use LEFT JOIN / RIGHT JOIN"),
    (r"\bCOMMIT\s*;", "COMMIT is not allowed inside a plain PL/pgSQL function - remove it"),
    (r"\bROLLBACK\s*;", "ROLLBACK is not allowed inside a plain PL/pgSQL function - remove it; "
                        "Postgres rolls back a function's changes automatically when it raises an exception"),
]

# ------------------------------------------------------------- MANUAL rules
# Looked for in the ORIGINAL Oracle code: if present, a person has to finish the job.

MANUAL_RULES = [
    (r"\bPRAGMA\b", "PRAGMA (e.g. AUTONOMOUS_TRANSACTION, EXCEPTION_INIT) has no Postgres equivalent"),
    (r"\bBULK\s+COLLECT\b|\bFORALL\b", "BULK COLLECT / FORALL have no direct equivalent - rewrite as set-based SQL or a loop"),
    (r"\bTYPE\s+\w+\s+IS\s+(TABLE|RECORD|VARRAY|REF)\b", "collection / record / ref types (TYPE ... IS ...) need a manual redesign"),
    (r"\bSYS_REFCURSOR\b|\bREF\s+CURSOR\b", "REF CURSOR / SYS_REFCURSOR need a manual rewrite (Postgres uses refcursor)"),
    (r"\bCONNECT\s+BY\b", "hierarchical query (CONNECT BY) must be rewritten with WITH RECURSIVE"),
    (r"\bPIPELINED\b|\bPIPE\s+ROW\b", "pipelined table functions have no equivalent - rewrite as RETURNS TABLE / SETOF"),
    (r"\b(UTL_\w+|DBMS_(?!OUTPUT)\w+)\b", "Oracle supplied packages (UTL_*, DBMS_* other than DBMS_OUTPUT) have no equivalent"),
]

# ----------------------------------------------------------- ADVISORY rules
# Looked for in the ORIGINAL; the translation may be perfectly valid and still differ.

ADVISORY_RULES = [
    (r"\|\|",
     "String concatenation: Oracle treats NULL as an empty string in || ('a' || NULL = 'a'); "
     "Postgres returns NULL. Use concat(a, b) or COALESCE where an operand can be NULL."),
    (r"(?<![/*])/(?![/*])",
     "Division: Oracle NUMBER division keeps decimals (7/2 = 3.5); Postgres integer/integer truncates "
     "(7/2 = 3). Check divisions whose operands are integers (cast one to NUMERIC)."),
    (r"(?:\bSYSDATE|\b\w*DATE\b|\b\w*_AT|\bDUE\w*)\s*[+-]\s*[\w(]",
     "Date arithmetic: in Oracle, date + number adds days and date - date is a number of days; in Postgres "
     "timestamp + integer is an error and timestamp - timestamp is an interval. Write date + n * INTERVAL '1 day', "
     "and (a::date - b::date) for a whole-day difference."),
    (r"\bSUBSTR\s*\(\s*[^,()]+,\s*0\s*,",
     "SUBSTR with start position 0: Oracle treats 0 as 1; Postgres substr(x, 0, n) returns n-1 characters."),
]
_EMPTY_STRING_LITERAL = re.compile(r"(?<![\w'])''(?!')")
_EMPTY_STRING_ADVICE = ("Empty strings: Oracle treats '' as NULL ('' IS NULL is true); Postgres does not. "
                        "Review comparisons against '' and checks for NULL on text values.")


def find_fix_problems(code: str) -> list:
    """Oracle-only constructs still present in TRANSLATED code (strings/comments ignored)."""
    body = code_only(code)
    return [message for pattern, message in FIX_RULES if re.search(pattern, body)]


def find_manual_constructs(original_plsql: str) -> list:
    body = code_only(original_plsql)
    return [message for pattern, message in MANUAL_RULES if re.search(pattern, body)]


def find_advisories(original_plsql: str) -> list:
    """Behaviour differences worth a human glance. Looks at the source without
    its trailing SQL*Plus '/' terminator, which is not code."""
    source = re.sub(r"\n\s*/\s*$", "", original_plsql.rstrip())
    body = code_only(source)
    found = [message for pattern, message in ADVISORY_RULES if re.search(pattern, body)]
    no_comments = re.sub(r"--[^\n]*", " ", re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL))
    if _EMPTY_STRING_LITERAL.search(no_comments):
        found.append(_EMPTY_STRING_ADVICE)
    return found


def select_into_without_strict(code: str) -> bool:
    """
    True if any SELECT ... INTO lacks STRICT. Verified on Postgres: a plain
    SELECT INTO returns NULL on zero rows and silently takes ONE row when
    there are several, where Oracle raises NO_DATA_FOUND / TOO_MANY_ROWS.

    Only a SELECT that is a STATEMENT counts: it starts the code or follows
    BEGIN / THEN / ELSE / LOOP / DECLARE. INSERT INTO ... SELECT, subqueries,
    cursor FETCH ... INTO and RETURNING ... INTO are different things.
    """
    for chunk in code_only(code).split(";"):
        m = re.search(r"\bSELECT\b", chunk)
        if not m:
            continue
        prefix = chunk[:m.start()].strip()
        if prefix and not re.search(r"\b(BEGIN|THEN|ELSE|LOOP|DECLARE)$", prefix):
            continue            # INSERT INTO t SELECT..., FOR r IN SELECT..., subquery
        if re.search(r"\bINTO\b(?!\s+STRICT\b)", chunk[m.start():]):
            return True
    return False
