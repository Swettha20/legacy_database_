"""
legacy-db-modernizer: reading an Oracle SQL script

Cuts a script written for Oracle's SQL*Plus (what `.sql` files from Oracle
tools look like) into the individual statements Oracle should run.

    - an ordinary statement ends at a semicolon
    - a PL/SQL unit (CREATE PROCEDURE / FUNCTION / PACKAGE / TRIGGER / TYPE,
      or a DECLARE / BEGIN block) contains semicolons of its own, so it ends
      at a line holding only a slash
    - a slash line also runs whatever is in the buffer, as in SQL*Plus
    - semicolons inside 'strings', q'[quoted strings]' and comments do not end
      anything; a string or a /* comment */ may span several lines
    - SQL*Plus's own commands (SET DEFINE OFF, PROMPT, SPOOL, @file ...) are
      not SQL: they are skipped and reported, never sent to Oracle

Pure text handling: no database, so it can be tested hard.
"""

import re
from dataclasses import dataclass

# SET <one of these> is a SQL*Plus setting; other SET statements
# (SET TRANSACTION, SET CONSTRAINTS, SET ROLE) are real SQL and are kept.
_SQLPLUS_SETTINGS = {
    "DEFINE", "SERVEROUTPUT", "ECHO", "FEEDBACK", "PAGESIZE", "PAGES", "LINESIZE", "LINES", "VERIFY", "VER",
    "TERMOUT", "SQLBLANKLINES", "TRIMSPOOL", "TRIMS", "HEADING", "HEAD", "TIMING", "TIME", "AUTOCOMMIT",
    "SQLTERMINATOR", "SQLT", "SCAN", "ESCAPE", "LONG", "LONGCHUNKSIZE", "NUMWIDTH", "NUMFORMAT", "NUM",
    "SQLPROMPT", "SQLNUMBER", "SQLPREFIX", "SQLCASE", "ARRAYSIZE", "ARRAY", "COLSEP", "UNDERLINE", "WRAP",
    "NEWPAGE", "SHOWMODE", "SUFFIX", "TAB", "TERM", "TRIM", "CMDSEP", "CONCAT", "DESCRIBE", "DOCUMENT",
    "ENCODING", "ERRORLOGGING", "FLAGGER", "FLUSH", "LOBOFFSET", "LOGSOURCE", "MARKUP", "NULL", "PAUSE",
    "RECSEP", "RECSEPCHAR", "SERVEROUT", "SPACE", "SQLCONTINUE", "STATEMENTCACHE", "XMLOPTIMIZATIONCHECK",
}
_SQLPLUS_COMMAND = re.compile(
    r"^(PROMPT|SPOOL|SHOW|WHENEVER|EXIT|QUIT|DEFINE|UNDEFINE|REM|REMARK|CONNECT|CONN|DISCONNECT|COLUMN|COL|"
    r"TTITLE|BTITLE|REPHEADER|REPFOOTER|CLEAR|ACCEPT|PAUSE|HOST|STORE|TIMING|VARIABLE|VAR|PRINT|DESCRIBE|DESC|"
    r"START|ED|EDIT|GET|SAVE|BREAK|COMPUTE)\b", re.IGNORECASE)
_PLSQL_START = re.compile(
    r"^(CREATE\s+(OR\s+REPLACE\s+)?((NON)?EDITIONABLE\s+)?(PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE)\b|DECLARE\b|BEGIN\b)",
    re.IGNORECASE)
_Q_CLOSERS = {"[": "]", "{": "}", "(": ")", "<": ">"}


@dataclass
class Statement:
    sql: str            # exactly what to send to Oracle (no trailing ; or /)
    plsql: bool         # a PL/SQL unit
    line: int           # line of the script it starts on (for error messages)

    @property
    def kind(self) -> str:
        words = re.sub(r"\s+", " ", self.sql.strip()).upper().split(" ")
        if words[0] == "CREATE":
            rest = [w for w in words[1:] if w not in ("OR", "REPLACE", "EDITIONABLE", "NONEDITIONABLE",
                                                       "UNIQUE", "BITMAP", "GLOBAL", "TEMPORARY", "MATERIALIZED")]
            return "CREATE " + (rest[0] if rest else "")
        return words[0]


def _sqlplus_command(line: str) -> bool:
    if line.startswith("@"):
        return True
    m = re.match(r"(?i)^SET\s+(\w+)", line)
    if m:
        return m.group(1).upper() in _SQLPLUS_SETTINGS
    return bool(_SQLPLUS_COMMAND.match(line))


def split_script(text: str):
    """Returns (statements, skipped_sqlplus_lines)."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    n = len(text)
    statements, skipped = [], []
    buf = []
    i, line_no = 0, 1
    state = None            # None | "'" | "--" | "/*" | "q"
    q_end = None
    code_seen = False
    plsql = False
    start_line = 1

    def finish():
        nonlocal buf, code_seen, plsql
        sql = "".join(buf).strip()
        if code_seen and sql:
            statements.append(Statement(sql, plsql, start_line))
        buf, code_seen, plsql = [], False, False

    while i < n:
        ch = text[i]

        # ---- at the start of a line, outside any string or comment
        if state is None and (i == 0 or text[i - 1] == "\n"):
            end = text.find("\n", i)
            end = n if end == -1 else end
            stripped = text[i:end].strip()
            if stripped == "/":
                finish()
                i, line_no = end + 1, line_no + 1
                continue
            if not code_seen:
                if stripped == "":
                    i, line_no = end + 1, line_no + 1
                    continue
                if _sqlplus_command(stripped):
                    skipped.append(stripped)
                    i, line_no = end + 1, line_no + 1
                    continue

        # ---- inside a string or comment
        if state == "'":
            buf.append(ch)
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    state = None
        elif state == "q":
            buf.append(ch)
            if ch == q_end and i + 1 < n and text[i + 1] == "'":
                buf.append("'")
                i += 1
                state = None
        elif state == "--":
            if ch == "\n":
                state = None
                if code_seen:
                    buf.append(ch)
            elif code_seen:
                buf.append(ch)
        elif state == "/*":
            if ch == "*" and i + 1 < n and text[i + 1] == "/":
                if code_seen:
                    buf.append("*/")
                i += 1
                state = None
            elif code_seen:
                buf.append(ch)

        # ---- ordinary text
        else:
            if ch == "-" and text[i:i + 2] == "--":
                state = "--"
                if code_seen:
                    buf.append("--")
                i += 1
            elif ch == "/" and text[i:i + 2] == "/*":
                state = "/*"
                if code_seen:
                    buf.append("/*")
                i += 1
            elif ch.isspace():
                if code_seen:
                    buf.append(ch)
            else:
                if not code_seen:
                    code_seen, start_line = True, line_no
                    plsql = bool(_PLSQL_START.match(text[i:i + 200]))
                if ch in "qQ" and text[i + 1:i + 2] == "'" and i + 2 < n and not text[i + 2].isspace() \
                        and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_$#")):
                    delimiter = text[i + 2]
                    q_end = _Q_CLOSERS.get(delimiter, delimiter)
                    buf.append(text[i:i + 3])
                    i += 2
                    state = "q"
                elif ch == "'":
                    buf.append(ch)
                    state = "'"
                elif ch == ";" and not plsql:
                    finish()
                else:
                    buf.append(ch)

        if ch == "\n":
            line_no += 1
        i += 1

    finish()
    return statements, skipped
