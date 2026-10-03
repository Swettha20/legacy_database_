"""
legacy-db-modernizer: constraint and default translation

Oracle -> PostgreSQL translation for the schema features that used to be
silently dropped: UNIQUE constraints, CHECK constraints, and column DEFAULTs.
(Before this module the tool only carried over primary keys, foreign keys,
and NOT NULL - so the migrated database accepted duplicate emails and
negative stock counts that the source Oracle database would have rejected.)

Design rule: translate only what is PROVABLY the same in both databases, and
flag everything else for human review instead of guessing. A wrong guess here
would be worse than a missing constraint, because it would look migrated.

This module is pure logic (no database, no network) so it can be tested hard.
"""

import re

# ---------------------------------------------------------------- helpers


def is_system_generated_name(name: str) -> bool:
    """Oracle names unnamed constraints SYS_C00123456 - not worth carrying over."""
    return bool(re.fullmatch(r"SYS_C\d+", name or "", re.IGNORECASE))


def is_not_null_check(condition: str) -> bool:
    """
    Oracle stores every NOT NULL as a CHECK constraint (`"NAME" IS NOT NULL`).
    The tool already migrates nullability separately, so these are not real
    CHECK constraints and must not be duplicated.
    """
    return bool(re.fullmatch(r'\s*"?[\w$#]+"?\s+IS\s+NOT\s+NULL\s*', condition or "", re.IGNORECASE))


# --------------------------------------------------------------- defaults

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_STRING = re.compile(r"'(?:[^']|'')*'")


def translate_default(expr):
    """
    Returns (pg_default, note).
      pg_default is the Postgres expression to use, or None for "no default".
      note is None when everything is fine, or a reason when a default existed
      in Oracle but could NOT be carried over (so the report can flag it).
    """
    if expr is None:
        return None, None
    text = expr.strip()
    if text == "" or text.upper() == "NULL":
        return None, None

    upper = text.upper()
    if upper in ("SYSDATE", "SYSTIMESTAMP", "CURRENT_TIMESTAMP", "CURRENT_DATE"):
        # SYSDATE includes the time of day, so NOW() - not CURRENT_DATE
        # (which would silently store midnight).
        return "NOW()", None
    if upper == "USER":
        return "CURRENT_USER", None
    if _NUMBER.fullmatch(text) or _STRING.fullmatch(text):
        return text, None

    return None, (f"Oracle default `{text}` has no verified Postgres equivalent - "
                  f"NOT carried over; set it manually if it matters")


# ----------------------------------------------------------------- checks

_KEYWORDS = {"AND", "OR", "NOT", "IN", "BETWEEN", "IS", "NULL", "LIKE"}

_TOKEN = re.compile(
    r"""\s*(?:
          (?P<str>'(?:[^']|'')*')
        | (?P<num>-?\d+(?:\.\d+)?)
        | (?P<op><>|!=|<=|>=|=|<|>)
        | (?P<punct>[(),])
        | (?P<ident>"[^"]+"|[A-Za-z_][A-Za-z0-9_$#]*)
    )""",
    re.VERBOSE,
)


def translate_check(condition, column_names):
    """
    Translates a simple Oracle CHECK condition (comparisons, AND/OR/NOT, IN,
    BETWEEN, LIKE, IS [NOT] NULL, over this table's own columns and literals).

    Returns (pg_condition, note):
      (condition, None)  - translated, safe to emit
      (None, reason)     - NOT translated; reason goes in the report

    Anything else - function calls, subqueries, arithmetic beyond a negative
    literal, unknown identifiers - is rejected on purpose. Division is
    excluded because it behaves differently (Oracle NUMBER division is
    decimal; Postgres integer division truncates).
    """
    columns = {c.lower() for c in column_names}
    text = (condition or "").strip()
    if not text:
        return None, "empty CHECK condition"

    out = []
    pos = 0
    previous = None   # last token kind, to catch "column(" function calls
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip() == "":
                break
            return None, f"unsupported syntax near `{text[pos:pos + 20].strip()}`"
        pos = m.end()

        if m.group("str") is not None:
            out.append(m.group("str")); previous = "literal"
        elif m.group("num") is not None:
            out.append(m.group("num")); previous = "literal"
        elif m.group("op") is not None:
            out.append(m.group("op")); previous = "op"
        elif m.group("punct") is not None:
            p = m.group("punct")
            if p == "(" and previous == "column":
                return None, "function call or unsupported expression in CHECK"
            out.append(p); previous = "punct" if p != ")" else "close"
        else:
            raw = m.group("ident")
            name = raw.strip('"')
            if name.upper() in _KEYWORDS and not raw.startswith('"'):
                out.append(name.upper()); previous = "keyword"
            elif name.lower() in columns:
                out.append(f'"{name.lower()}"'); previous = "column"
            else:
                return None, f"`{name}` is not a column of this table (function or unsupported construct)"

    pg = " ".join(out)
    pg = pg.replace("( ", "(").replace(" )", ")").replace(" ,", ",")
    return pg, None
