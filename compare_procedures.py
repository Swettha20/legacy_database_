"""
legacy-db-modernizer: differential checker

Every other check in this project looks at the translated code. This one
checks what it DOES: the same call is made against the Oracle original and
the Postgres translation, on the same data, and everything observable is
compared:

  * the return value (functions) or the OUT parameters (procedures)
  * the error, if any - Oracle's RAISE_APPLICATION_ERROR text against the
    translated RAISE EXCEPTION text, and Oracle's standard errors against the
    matching Postgres error class (no data found, unique violation ...)
  * what the code printed (DBMS_OUTPUT against RAISE NOTICE)
  * its side effects, by running the SELECTs you list under "observe" in
    both databases after the call

Each call runs in a transaction that is rolled back on both sides, so your
data is left alone - with one exception it refuses by default: an Oracle
original that COMMITs (or is an autonomous transaction) cannot be rolled
back, so it is skipped unless you pass --allow-commit.

    python compare_procedures.py                       # uses procedure_tests.json
    python compare_procedures.py --tests my_tests.json
    python compare_procedures.py --allow-commit

A test file looks like:

    {"tables": ["members", "books", "loans"],        # optional: row counts are compared first
     "time_tolerance_seconds": 5,                    # optional: SYSDATE vs NOW() differ by moments
     "cases": [
        {"call": "member_loan_count", "args": [1], "returns": "number"},
        {"call": "add_member_safe", "args": ["T", "a@x.com", {"out": "number"}]},
        {"call": "renew_loan", "args": [1, 5],
         "observe": ["SELECT id, due_date FROM loans ORDER BY id"]}
     ]}

"returns" (number | string | date) marks a FUNCTION and gives the type of its
result; a case without it is a PROCEDURE. {"out": type} marks an OUT parameter.
An observe entry may also be {"oracle": "...", "postgres": "..."} when the SQL
differs between the databases.

What this proves and what it does not: it proves the two behave the same FOR
THE CALLS YOU LISTED. It cannot prove equivalence for inputs you did not try,
so cover the edge cases (no rows, bad input, the error branches).
"""

import argparse
import datetime
import decimal
import json
import re
import sys
from dataclasses import dataclass, field

DEFAULT_TOLERANCE_SECONDS = 5

# Oracle's standard errors -> the Postgres SQLSTATE a correct translation raises.
# RAISE_APPLICATION_ERROR (-20000..-20999) is handled separately: it must become
# RAISE EXCEPTION (SQLSTATE P0001) with the same message.
ORACLE_TO_SQLSTATE = {
    1403: "P0002",    # no data found            -> no_data_found (needs SELECT ... INTO STRICT)
    1422: "P0003",    # exact fetch > 1 row      -> too_many_rows (needs SELECT ... INTO STRICT)
    1: "23505",       # unique constraint        -> unique_violation
    1400: "23502",    # cannot insert NULL       -> not_null_violation
    2291: "23503",    # parent key not found     -> foreign_key_violation
    2292: "23503",    # child record found       -> foreign_key_violation
    2290: "23514",    # check constraint         -> check_violation
    1476: "22012",    # divisor is zero          -> division_by_zero
    1722: "22P02",    # invalid number           -> invalid_text_representation
    12899: "22001",   # value too large          -> string_data_right_truncation
    1438: "22003",    # value > precision        -> numeric_value_out_of_range
    942: "42P01",     # table does not exist     -> undefined_table
    904: "42703",     # invalid identifier       -> undefined_column
}

_NAME = re.compile(r"^[A-Za-z_][\w$]*$")


@dataclass
class Outcome:
    value: object = None
    outs: list = field(default_factory=list)
    error: dict = None                      # {"class": ..., "message": ..., "native": ...}
    notices: list = field(default_factory=list)
    observations: list = field(default_factory=list)


@dataclass
class CaseResult:
    case: dict
    status: str                             # SAME | DIFFERENT | SKIPPED | ERROR
    differences: list = field(default_factory=list)
    detail: str = ""
    oracle: Outcome = None
    postgres: Outcome = None


def _is_out(arg) -> bool:
    return isinstance(arg, dict) and "out" in arg


def describe_call(case: dict) -> str:
    shown = ", ".join("OUT" if _is_out(a) else repr(a) for a in case.get("args", []))
    return f"{case['call']}({shown})"


# ------------------------------------------------------------- comparison


def _to_decimal(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, decimal.Decimal)):
        return decimal.Decimal(value)
    if isinstance(value, float):
        return decimal.Decimal(repr(value))
    return None


def same_value(a, b, tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS) -> bool:
    """Are two values from the two databases 'the same' for our purposes?
    Numbers compare numerically (2 == Decimal('2.00')); date/times compare
    within a tolerance (SYSDATE and NOW() are never the same instant);
    everything else must match exactly."""
    if a is None or b is None:
        return a is None and b is None
    na, nb = _to_decimal(a), _to_decimal(b)
    if na is not None and nb is not None:
        return abs(na - nb) <= decimal.Decimal("1e-9")
    if isinstance(a, (datetime.datetime, datetime.date)) and isinstance(b, (datetime.datetime, datetime.date)):
        a = a if isinstance(a, datetime.datetime) else datetime.datetime.combine(a, datetime.time())
        b = b if isinstance(b, datetime.datetime) else datetime.datetime.combine(b, datetime.time())
        if (a.tzinfo is None) != (b.tzinfo is None):
            a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
        return abs((a - b).total_seconds()) <= tolerance_seconds
    return a == b


def _normalise_message(text: str) -> str:
    return " ".join((text or "").split())


def compare_outcomes(case: dict, oracle: Outcome, postgres: Outcome, tolerance: float) -> list:
    """The list of differences between the two outcomes; empty means identical."""
    found = []

    if oracle.error or postgres.error:
        if bool(oracle.error) != bool(postgres.error):
            who, other = ("Oracle", "Postgres") if oracle.error else ("Postgres", "Oracle")
            err = oracle.error or postgres.error
            found.append(f"{who} raised an error but {other} did not: {err['native']}: {err['message']}")
        else:
            o, p = oracle.error, postgres.error
            if o["class"] != p["class"]:
                found.append(f"different kind of error: Oracle {o['native']} ({o['message']}) vs "
                             f"Postgres SQLSTATE {p['class']} ({p['message']})")
            elif o["class"] == "P0001" and _normalise_message(o["message"]) != _normalise_message(p["message"]):
                found.append(f"error message differs: Oracle {o['message']!r} vs Postgres {p['message']!r}")
    else:
        if "returns" in case and not same_value(oracle.value, postgres.value, tolerance):
            found.append(f"return value differs: Oracle {oracle.value!r} vs Postgres {postgres.value!r}")
        if len(oracle.outs) != len(postgres.outs):
            found.append(f"different number of OUT values: Oracle {oracle.outs!r} vs Postgres {postgres.outs!r}")
        else:
            for i, (a, b) in enumerate(zip(oracle.outs, postgres.outs), 1):
                if not same_value(a, b, tolerance):
                    found.append(f"OUT parameter {i} differs: Oracle {a!r} vs Postgres {b!r}")

    if [_normalise_message(n) for n in oracle.notices] != [_normalise_message(n) for n in postgres.notices]:
        found.append(f"printed output differs: Oracle {oracle.notices!r} vs Postgres {postgres.notices!r}")

    for i, (orows, prows) in enumerate(zip(oracle.observations, postgres.observations), 1):
        if len(orows) != len(prows):
            found.append(f"observed query {i}: {len(orows)} rows in Oracle vs {len(prows)} in Postgres")
            continue
        for r, (orow, prow) in enumerate(zip(orows, prows), 1):
            if len(orow) != len(prow) or not all(same_value(a, b, tolerance) for a, b in zip(orow, prow)):
                found.append(f"observed query {i}, row {r} differs: Oracle {tuple(orow)!r} vs Postgres {tuple(prow)!r}")
                break
    return found


# ----------------------------------------------------------------- runners


def _observe_sql(entry, side: str) -> str:
    return entry[side] if isinstance(entry, dict) else entry


class PostgresRunner:
    def __init__(self, connection):
        self.conn = connection

    def run(self, case: dict) -> Outcome:
        import psycopg2
        name = case["call"]
        if not _NAME.match(name):
            raise ValueError(f"unsafe procedure name: {name!r}")
        args = case.get("args", [])
        in_args = [a for a in args if not _is_out(a)]
        has_out = any(_is_out(a) for a in args)
        placeholders = ", ".join(["%s"] * len(in_args))
        outcome = Outcome()
        del self.conn.notices[:]
        cur = self.conn.cursor()
        try:
            if has_out:
                cur.execute(f"SELECT * FROM {name}({placeholders})", in_args)
                outcome.outs = list(cur.fetchone())
            else:
                cur.execute(f"SELECT {name}({placeholders})", in_args)
                row = cur.fetchone()
                if "returns" in case:
                    outcome.value = row[0]
            for entry in case.get("observe", []):
                cur.execute(_observe_sql(entry, "postgres"))
                outcome.observations.append(cur.fetchall())
        except psycopg2.Error as e:
            outcome.error = {
                "class": e.pgcode,
                "message": (e.diag.message_primary if e.diag and e.diag.message_primary else str(e)).strip(),
                "native": f"SQLSTATE {e.pgcode}",
            }
        finally:
            self.conn.rollback()
        outcome.notices = [n.replace("NOTICE:", "", 1).strip() for n in self.conn.notices]
        return outcome


class OracleRunner:
    def __init__(self, connection):
        self.conn = connection

    @staticmethod
    def _bind_type(kind: str):
        import oracledb
        return {"number": oracledb.DB_TYPE_NUMBER, "string": str, "date": oracledb.DB_TYPE_DATE}[kind]

    @staticmethod
    def parse_error(exc) -> dict:
        err = exc.args[0] if exc.args else None
        code = getattr(err, "code", None)
        message = getattr(err, "message", None) or str(exc)
        text = re.sub(r"^ORA-\d+:\s*", "", message.splitlines()[0].strip())
        native = f"ORA-{code:05d}" if isinstance(code, int) else "ORA-?????"
        if isinstance(code, int) and 20000 <= code <= 20999:
            return {"class": "P0001", "message": text, "native": native}
        return {"class": ORACLE_TO_SQLSTATE.get(code, native), "message": text, "native": native}

    def _drain_output(self, cur) -> list:
        lines, line, status = [], cur.var(str), cur.var(int)
        while True:
            cur.callproc("dbms_output.get_line", (line, status))
            if status.getvalue() != 0:
                return lines
            lines.append(line.getvalue() or "")

    def run(self, case: dict) -> Outcome:
        import oracledb
        name = case["call"]
        if not _NAME.match(name):
            raise ValueError(f"unsafe procedure name: {name!r}")
        cur = self.conn.cursor()
        binds, refs, outs = {}, [], []
        for i, arg in enumerate(case.get("args", [])):
            key = f"a{i}"
            if _is_out(arg):
                binds[key] = cur.var(self._bind_type(arg["out"]))
                outs.append(key)
            else:
                binds[key] = arg
            refs.append(f":{key}")
        call = f"{name}({', '.join(refs)})"
        if "returns" in case:
            binds["ret"] = cur.var(self._bind_type(case["returns"]))
            block = f"BEGIN :ret := {call}; END;"
        else:
            block = f"BEGIN {call}; END;"

        outcome = Outcome()
        cur.callproc("dbms_output.enable", (None,))
        try:
            cur.execute(block, binds)
            if "returns" in case:
                outcome.value = binds["ret"].getvalue()
            outcome.outs = [binds[k].getvalue() for k in outs]
            outcome.notices = self._drain_output(cur)
            for entry in case.get("observe", []):
                cur.execute(_observe_sql(entry, "oracle"))
                outcome.observations.append(cur.fetchall())
        except oracledb.DatabaseError as e:
            outcome.error = self.parse_error(e)
            try:
                outcome.notices = self._drain_output(cur)
            except oracledb.DatabaseError:
                pass
        finally:
            self.conn.rollback()
        return outcome


# --------------------------------------------------------- orchestration


def run_cases(cases, oracle_runner, pg_runner, sources: dict, allow_commit: bool, tolerance: float) -> list:
    """sources maps UPPERCASE procedure name -> its Oracle source text."""
    from plsql_rules import code_only
    results = []
    for case in cases:
        name = case["call"].upper()
        source = sources.get(name)
        if source is None:
            results.append(CaseResult(case, "ERROR", detail=f"{name} was not found in the Oracle schema"))
            continue
        body = code_only(source)
        if not allow_commit and re.search(r"\bCOMMIT\b|\bAUTONOMOUS_TRANSACTION\b", body):
            results.append(CaseResult(case, "SKIPPED", detail=(
                "the Oracle original COMMITs, which cannot be rolled back, so running it would change "
                "your Oracle data - re-run with --allow-commit to run it for real (then re-migrate)")))
            continue
        try:
            oracle, postgres = oracle_runner.run(case), pg_runner.run(case)
        except Exception as e:
            results.append(CaseResult(case, "ERROR", detail=f"could not run the call: {e}"))
            continue
        differences = compare_outcomes(case, oracle, postgres, tolerance)
        results.append(CaseResult(case, "DIFFERENT" if differences else "SAME", differences,
                                  oracle=oracle, postgres=postgres))
    return results


def compare_table_counts(oracle_conn, pg_conn, tables: list) -> list:
    """Warnings if the two databases do not hold the same number of rows -
    a comparison is meaningless if they started from different data."""
    warnings = []
    ocur, pcur = oracle_conn.cursor(), pg_conn.cursor()
    for table in tables:
        if not _NAME.match(table):
            continue
        ocur.execute(f'SELECT COUNT(*) FROM "{table.upper()}"')
        pcur.execute(f'SELECT COUNT(*) FROM "{table.lower()}"')
        o, p = ocur.fetchone()[0], pcur.fetchone()[0]
        if o != p:
            warnings.append(f"{table}: {o} rows in Oracle but {p} in Postgres")
    pg_conn.rollback()
    return warnings


def format_report(results: list) -> str:
    lines = []
    for r in results:
        lines.append(f"  {r.status:10} {describe_call(r.case)}" + (f"   # {r.case['note']}" if r.case.get("note") else ""))
        if r.detail:
            lines.append(f"             {r.detail}")
        for d in r.differences:
            lines.append(f"             - {d}")
    counts = {s: sum(1 for r in results if r.status == s) for s in ("SAME", "DIFFERENT", "SKIPPED", "ERROR")}
    lines.append("")
    lines.append(f"  {counts['SAME']} identical, {counts['DIFFERENT']} different, "
                 f"{counts['SKIPPED']} skipped, {counts['ERROR']} could not run")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare Oracle procedures with their Postgres translations.")
    parser.add_argument("--tests", default="procedure_tests.json")
    parser.add_argument("--allow-commit", action="store_true",
                        help="also run Oracle originals that COMMIT (their changes cannot be rolled back)")
    parser.add_argument("--tolerance", type=float, help="seconds two date/times may differ by")
    args = parser.parse_args(argv)

    try:
        with open(args.tests, encoding="utf-8") as f:
            spec = json.load(f)
    except (OSError, ValueError) as e:
        print(f"ERROR: could not read {args.tests} - {e}")
        return 1
    cases = spec.get("cases", [])
    tolerance = args.tolerance if args.tolerance is not None else spec.get("time_tolerance_seconds", DEFAULT_TOLERANCE_SECONDS)

    try:
        import psycopg2
        from config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME
        from read_schema import get_oracle_connection, read_source_objects
        sources = {o["name"].upper(): o["source"] for o in read_source_objects()}
        oracle_conn = get_oracle_connection()
        pg_conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=PG_DBNAME)
    except Exception as e:
        print(f"ERROR: could not connect to both databases - {e}")
        return 1

    try:
        if spec.get("tables"):
            for warning in compare_table_counts(oracle_conn, pg_conn, spec["tables"]):
                print(f"WARNING: the databases hold different data - {warning}")
        print(f"Comparing {len(cases)} calls (date/times may differ by up to {tolerance}s):\n")
        results = run_cases(cases, OracleRunner(oracle_conn), PostgresRunner(pg_conn), sources,
                            args.allow_commit, tolerance)
        print(format_report(results))
        return 1 if any(r.status in ("DIFFERENT", "ERROR") for r in results) else 0
    finally:
        oracle_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    sys.exit(main())
