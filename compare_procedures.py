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
import time
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
    retried: bool = False                   # the first attempt timed out and a second one was made


def _is_out(arg) -> bool:
    return isinstance(arg, dict) and "out" in arg


def decode_arg(arg):
    """JSON cannot hold a date, so {"date": "2026-10-20 09:30:00"} stands for one."""
    if isinstance(arg, dict) and "date" in arg:
        return datetime.datetime.fromisoformat(arg["date"])
    return arg


def describe_call(case: dict) -> str:
    def show(a):
        if _is_out(a):
            return "OUT"
        if isinstance(a, dict) and "date" in a:
            return f"date({a['date']!r})"
        return repr(a)
    shown = ", ".join(show(a) for a in case.get("args", []))
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
        in_args = [decode_arg(a) for a in args if not _is_out(a)]
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
        except psycopg2.Error as e:
            if is_timeout(e):
                raise                          # the database was stuck; that says nothing about the procedure
            outcome.error = {
                "class": e.pgcode,
                "message": (e.diag.message_primary if e.diag and e.diag.message_primary else str(e)).strip(),
                "native": f"SQLSTATE {e.pgcode}",
            }
        else:
            for entry in case.get("observe", []):
                sql = _observe_sql(entry, "postgres")
                try:
                    cur.execute(sql)
                    outcome.observations.append(cur.fetchall())
                except psycopg2.Error as e:
                    if is_timeout(e):
                        raise
                    raise observation_failed(e, sql)
        finally:
            self.conn.rollback()
        outcome.notices = [n.replace("NOTICE:", "", 1).strip() for n in self.conn.notices]
        return outcome


class OracleRunner:
    def __init__(self, connection, schema: str = None):
        self.conn = connection
        self.schema = schema          # set when the code lives in a schema other than the login user's

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
                binds[key] = decode_arg(arg)
            refs.append(f":{key}")
        qualified = f"{self.schema}.{name}" if self.schema else name
        call = f"{qualified}({', '.join(refs)})"
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
        except oracledb.DatabaseError as e:
            if is_timeout(e):
                raise                          # the database was stuck; that says nothing about the procedure
            outcome.error = self.parse_error(e)
            try:
                outcome.notices = self._drain_output(cur)
            except oracledb.DatabaseError:
                pass
        else:
            for entry in case.get("observe", []):
                sql = _observe_sql(entry, "oracle")
                try:
                    cur.execute(sql)
                    outcome.observations.append(cur.fetchall())
                except oracledb.DatabaseError as e:
                    if is_timeout(e):
                        raise
                    raise observation_failed(e, sql)
        finally:
            self.conn.rollback()
            try:
                cur.close()                    # an unclosed cursor stays alive on the connection
            except Exception:
                pass
        return outcome


class FreshConnectionOracleRunner:
    """
    Runs every case on its own brand-new Oracle connection, closed afterwards.

    Why: on a real run, the tenth call stalled when it followed nine others on one
    shared connection, yet the very same call ran instantly alone and after just one
    other case. Something built up in the shared connection (the driver's statement
    cache and open cursors, or the session). A new connection per case means no
    state can carry from one case to the next. It costs a fraction of a second each.
    """

    def __init__(self, connect, schema: str = None, timeout_seconds: int = None):
        self.connect, self.schema, self.timeout_seconds = connect, schema, timeout_seconds

    def run(self, case: dict) -> Outcome:
        conn = self.connect()
        try:
            conn.call_timeout = (self.timeout_seconds or CALL_TIMEOUT_SECONDS) * 1000
            if self.schema:
                use_oracle_schema(conn, self.schema)
            return OracleRunner(conn, self.schema).run(case)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def choose_oracle_runner(reuse_connection: bool, oracle_conn, schema, connect):
    return OracleRunner(oracle_conn, schema) if reuse_connection else FreshConnectionOracleRunner(connect, schema)


# --------------------------------------------------------- orchestration


CALL_TIMEOUT_SECONDS = 30       # one call on either database may not take longer than this

# SQLSTATEs that mean "the database could not run it in time" - not something the procedure did.
PG_TIMEOUT_STATES = ("57014", "55P03")          # query_canceled (statement_timeout), lock_not_available


class ObservationError(RuntimeError):
    """An observe query failed. That is a problem with the test or the connection, NOT something
    the procedure did, so it must never be recorded as the procedure's behaviour."""


def observation_failed(error: Exception, sql: str) -> ObservationError:
    text = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    return ObservationError(f"an observe query failed ({text}); the query was: {' '.join(sql.split())[:120]}")


_SCHEMA_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,29}$")


def use_oracle_schema(connection, schema: str):
    """Make unqualified table names in observe queries mean the sample's schema, not the login user's."""
    if not _SCHEMA_NAME.match(schema or ""):
        raise ValueError(f"unsafe schema name: {schema!r}")
    cur = connection.cursor()
    try:
        cur.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {schema}")
    finally:
        cur.close()


def is_timeout(error: Exception) -> bool:
    try:
        import psycopg2.errors as pg_errors
        if isinstance(error, (pg_errors.QueryCanceled, pg_errors.LockNotAvailable)):
            return True
    except ImportError:
        pass
    text = str(error).lower()
    return (getattr(error, "pgcode", None) in PG_TIMEOUT_STATES
            or any(w in text for w in ("dpy-4024", "ora-01013", "call timeout")))

TIMEOUT_HINT = ("it took longer than the limit - most likely another program or window is holding a lock on the "
                "same tables (a SQL tool left connected, or an earlier run that was not closed), or the machine is "
                "overloaded. Close other connections to these databases and run it again")


def explain_error(error: Exception) -> str:
    text = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    lowered = text.lower()
    if any(w in lowered for w in ("timeout", "timed out", "canceling statement", "dpy-4024", "ora-01013")):
        return f"{text} ({TIMEOUT_HINT})"
    return text


def run_cases(cases, oracle_runner, pg_runner, sources: dict, allow_commit: bool, tolerance: float,
              progress=None, say=None, retry_timeouts: bool = True) -> list:
    """sources maps UPPERCASE procedure name -> its Oracle source text.
    `progress`, if given, is called as progress(number, total, result, seconds) after each case."""
    from plsql_rules import code_only
    results = []
    total = len(cases)
    for case in cases:
        started = time.time()
        name = case["call"].upper()
        source = sources.get(name)
        if source is None:
            results.append(CaseResult(case, "ERROR", detail=f"{name} was not found in the Oracle schema"))
            _report(progress, results, total, started)
            continue
        body = code_only(source)
        if not allow_commit and re.search(r"\bCOMMIT\b|\bAUTONOMOUS_TRANSACTION\b", body):
            results.append(CaseResult(case, "SKIPPED", detail=(
                "the Oracle original COMMITs, which cannot be rolled back, so running it would change "
                "your Oracle data - re-run with --allow-commit to run it for real (then re-migrate)")))
            _report(progress, results, total, started)
            continue
        oracle, postgres, retried, error = _run_with_retry(case, oracle_runner, pg_runner, retry_timeouts, say)
        if error is not None:
            results.append(CaseResult(case, "ERROR", detail=f"could not run the call: {explain_error(error)}",
                                      retried=retried))
        else:
            differences = compare_outcomes(case, oracle, postgres, tolerance)
            results.append(CaseResult(case, "DIFFERENT" if differences else "SAME", differences,
                                      oracle=oracle, postgres=postgres, retried=retried))
        _report(progress, results, total, started)
    return results


def _run_with_retry(case, oracle_runner, pg_runner, retry: bool, say):
    """(oracle, postgres, retried, error). `error` is set only if the call could not be run.
    A timeout is retried once: every case is rolled back, so running it again is safe, and a
    one-off stall then costs 30 seconds while the case still completes."""
    retried = False
    while True:
        try:
            return oracle_runner.run(case), pg_runner.run(case), retried, None
        except Exception as e:
            if is_timeout(e) and retry and not retried:
                retried = True
                if say:
                    say(f"  ... {describe_call(case)} timed out after {CALL_TIMEOUT_SECONDS}s - trying once more")
                continue
            return None, None, retried, e


def _report(progress, results: list, total: int, started: float):
    if progress:
        progress(len(results), total, results[-1], time.time() - started)


def compare_table_counts(oracle_conn, pg_conn, tables: list, schema: str = None) -> list:
    """Warnings if the two databases do not hold the same number of rows -
    a comparison is meaningless if they started from different data."""
    warnings = []
    ocur, pcur = oracle_conn.cursor(), pg_conn.cursor()
    for table in tables:
        if not _NAME.match(table):
            continue
        owner = f'"{schema}".' if schema else ""
        ocur.execute(f'SELECT COUNT(*) FROM {owner}"{table.upper()}"')
        pcur.execute(f'SELECT COUNT(*) FROM "{table.lower()}"')
        o, p = ocur.fetchone()[0], pcur.fetchone()[0]
        if o != p:
            warnings.append(f"{table}: {o} rows in Oracle but {p} in Postgres")
    pg_conn.rollback()
    return warnings


def print_progress(number: int, total: int, result, seconds: float):
    """One line per call, the moment it finishes, so a slow or stuck call is visible."""
    extra = ", needed a second try" if result.retried else ""
    print(f"  [{number}/{total}] {describe_call(result.case)}  ->  {result.status}  ({seconds:.1f}s{extra})", flush=True)


def numbered_progress(numbers: list, total: int):
    """Progress lines carry the case's number in the test file, so `--only 7` still says [7/13]."""
    return lambda n, _t, result, seconds: print_progress(numbers[n - 1], total, result, seconds)


def format_report(results: list) -> str:
    lines = []
    for r in results:
        lines.append(f"  {r.status:10} {describe_call(r.case)}" + (f"   # {r.case['note']}" if r.case.get("note") else ""))
        if r.detail:
            lines.append(f"             {r.detail}")
        if r.retried and r.status in ("SAME", "DIFFERENT"):
            lines.append(f"             (the first attempt timed out and the second one worked - worth knowing: something stalled once)")
        for d in r.differences:
            lines.append(f"             - {d}")
    counts = {s: sum(1 for r in results if r.status == s) for s in ("SAME", "DIFFERENT", "SKIPPED", "ERROR")}
    lines.append("")
    lines.append(f"  {counts['SAME']} identical, {counts['DIFFERENT']} different, "
                 f"{counts['SKIPPED']} skipped, {counts['ERROR']} could not run")
    return "\n".join(lines)


def parse_only(text: str, total: int) -> list:
    """'7' or '7,10' or '3-5' -> [7] / [7, 10] / [3, 4, 5] (1-based, in the order of the test file)."""
    chosen = []
    for part in text.split(","):
        part = part.strip()
        low, dash, high = part.partition("-")
        if not (low.strip().isdigit() and (not dash or high.strip().isdigit())):
            raise ValueError(f"'{part}' is not a case number (use e.g. 7, or 7,10, or 3-5)")
        first, last = int(low), int(high) if dash else int(low)
        if first < 1 or last > total or first > last:
            raise ValueError(f"'{part}' is outside 1-{total}: this test file has {total} cases")
        chosen.extend(n for n in range(first, last + 1) if n not in chosen)
    return chosen


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare Oracle procedures with their Postgres translations.")
    parser.add_argument("--tests", default="procedure_tests.json")
    parser.add_argument("--allow-commit", action="store_true",
                        help="also run Oracle originals that COMMIT (their changes cannot be rolled back)")
    parser.add_argument("--tolerance", type=float, help="seconds two date/times may differ by")
    parser.add_argument("--only", help="run only these cases, by number: 7, or 7,10, or 3-5 (see --list)")
    parser.add_argument("--list", action="store_true", help="show the numbered cases and exit")
    parser.add_argument("--reuse-connection", action="store_true",
                        help="run all Oracle calls on ONE connection (the old behaviour; for diagnosis)")
    args = parser.parse_args(argv)

    try:
        with open(args.tests, encoding="utf-8") as f:
            spec = json.load(f)
    except (OSError, ValueError) as e:
        print(f"ERROR: could not read {args.tests} - {e}")
        return 1
    cases = spec.get("cases", [])
    if args.list:
        for number, case in enumerate(cases, 1):
            print(f"{number:>3}. {describe_call(case)}" + (f"   - {case['note']}" if case.get("note") else ""))
        return 0
    numbers = list(range(1, len(cases) + 1))
    if args.only:
        try:
            numbers = parse_only(args.only, len(cases))
        except ValueError as e:
            print(f"ERROR: {e}")
            return 2
    total_cases = len(cases)
    cases = [cases[n - 1] for n in numbers]
    tolerance = args.tolerance if args.tolerance is not None else spec.get("time_tolerance_seconds", DEFAULT_TOLERANCE_SECONDS)

    try:
        import psycopg2
        from config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME, ORACLE_USER, ORACLE_SCHEMA
        from read_schema import get_oracle_connection, read_source_objects
        schema = ORACLE_SCHEMA if ORACLE_SCHEMA != ORACLE_USER.upper() else None
        sources = {o["name"].upper(): o["source"] for o in read_source_objects()}
        oracle_conn = get_oracle_connection()
        oracle_conn.call_timeout = CALL_TIMEOUT_SECONDS * 1000
        if schema:
            use_oracle_schema(oracle_conn, schema)
        pg_conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=PG_DBNAME,
                                   options=f"-c statement_timeout={CALL_TIMEOUT_SECONDS * 1000} "
                                           f"-c lock_timeout={CALL_TIMEOUT_SECONDS * 1000}")
    except Exception as e:
        print(f"ERROR: could not connect to both databases - {e}")
        return 1

    try:
        if spec.get("tables"):
            for warning in compare_table_counts(oracle_conn, pg_conn, spec["tables"], schema):
                print(f"WARNING: the databases hold different data - {warning}")
        print(f"Comparing {len(cases)} call{'s' if len(cases) != 1 else ''} "
              f"(date/times may differ by up to {tolerance}s):\n")
        results = run_cases(cases, choose_oracle_runner(args.reuse_connection, oracle_conn, schema, get_oracle_connection),
                            PostgresRunner(pg_conn), sources,
                            args.allow_commit, tolerance,
                            progress=numbered_progress(numbers, total_cases), say=print)
        print()
        print(format_report(results))
        return 1 if any(r.status in ("DIFFERENT", "ERROR") for r in results) else 0
    finally:
        oracle_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    sys.exit(main())
