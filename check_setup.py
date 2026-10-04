"""
legacy-db-modernizer: one-command health check

    python check_setup.py

Answers "is everything in working order?" without having to remember a dozen
commands. It never changes anything - every check is read-only (the AI check
sends one tiny prompt). Each line is one of:

    PASS   it is as it should be
    WARN   worth a look, but not necessarily wrong
    FAIL   something is wrong; the line after it says what to do
    SKIP   could not be checked (usually because something it needs failed)

The exit code is 1 if anything FAILed, so it can also be used in a script.

    python check_setup.py --skip-ai       # do not call the AI provider
    python check_setup.py --skip-docker   # do not look at the containers
"""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class Check:
    status: str            # PASS | WARN | FAIL | SKIP
    title: str
    detail: str = ""
    hint: str = ""


def _first_line(exc) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:160]


# ------------------------------------------------------------------ checks


def check_config() -> Check:
    try:
        import config  # noqa: F401  (raises with a clear message if a password is missing)
        return Check("PASS", "configuration (.env) loads",
                     f"schema {config.ORACLE_SCHEMA}, Postgres database {config.PG_DBNAME}")
    except Exception as e:
        return Check("FAIL", "configuration (.env) loads", _first_line(e),
                     "create a .env file next to the scripts (copy .env.example) and fill in the passwords")


def check_ai() -> Check:
    try:
        from llm_provider import ask_llm, provider_name
        reply = ask_llm("Reply with exactly one word: pong", timeout=30).strip()
        return Check("PASS", f"AI provider answers ({provider_name()})", f"replied {reply[:30]!r}")
    except Exception as e:
        return Check("FAIL", "AI provider answers", _first_line(e),
                     "run: python llm_provider.py   (and, for Groq, python llm_provider.py --models)")


def connect_oracle():
    from read_schema import get_oracle_connection
    conn = get_oracle_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM dual")
    cur.fetchone()
    return conn


def connect_postgres():
    import psycopg2
    from config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME
    return psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD,
                            dbname=PG_DBNAME, connect_timeout=5)


def check_tables_exist(oracle_names: list, pg_conn) -> Check:
    cur = pg_conn.cursor()
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
    in_pg = {r[0] for r in cur.fetchall()}
    missing = [t for t in oracle_names if t.lower() not in in_pg]
    if missing:
        return Check("FAIL", "every Oracle table exists in Postgres", f"missing: {', '.join(missing)}",
                     "run: python write_postgres_schema.py   (then python migrate_data.py)")
    return Check("PASS", "every Oracle table exists in Postgres", f"{len(oracle_names)} tables")


def check_row_counts(oracle_conn, pg_conn, oracle_names: list) -> Check:
    from config import ORACLE_SCHEMA
    ocur, pcur = oracle_conn.cursor(), pg_conn.cursor()
    bad = []
    for table in oracle_names:
        ocur.execute(f'SELECT COUNT(*) FROM "{ORACLE_SCHEMA}"."{table}"')
        pcur.execute(f'SELECT COUNT(*) FROM "{table.lower()}"')
        o, p = ocur.fetchone()[0], pcur.fetchone()[0]
        if o != p:
            bad.append(f"{table}: {o} in Oracle, {p} in Postgres")
    pg_conn.rollback()
    if bad:
        return Check("FAIL", "row counts match", "; ".join(bad), "run: python migrate_data.py")
    return Check("PASS", "row counts match", ", ".join(oracle_names))


def check_constraints(oracle_conn, pg_conn, oracle_names: list) -> Check:
    """UNIQUE, foreign-key and CHECK counts: what Oracle defines vs what Postgres holds."""
    from read_schema import get_columns, get_unique_constraints, get_foreign_keys, get_check_constraints
    from constraints import translate_check
    cur, pcur = oracle_conn.cursor(), pg_conn.cursor()
    problems, untranslatable = [], 0
    for table in oracle_names:
        cols = [c["name"] for c in get_columns(cur, table)]
        expected_u = len(get_unique_constraints(cur, table))
        expected_f = len(get_foreign_keys(cur, table))
        checks = get_check_constraints(cur, table)
        expected_c = sum(1 for c in checks if translate_check(c["condition"], cols)[0])
        untranslatable += len(checks) - expected_c
        pcur.execute("SELECT contype, count(*) FROM pg_constraint WHERE conrelid = %s::regclass "
                     "AND contype IN ('u','f','c') GROUP BY contype", (f'"{table.lower()}"',))
        have = dict(pcur.fetchall())
        for label, kind, want in (("UNIQUE", "u", expected_u), ("foreign key", "f", expected_f), ("CHECK", "c", expected_c)):
            if have.get(kind, 0) != want:
                problems.append(f"{table.lower()}: {want} {label} expected, {have.get(kind, 0)} in Postgres")
    pg_conn.rollback()
    if problems:
        return Check("FAIL", "UNIQUE / foreign key / CHECK constraints are all in Postgres", "; ".join(problems),
                     "re-run the pipeline: build_ir.py, type_mapper.py, write_postgres_schema.py, migrate_data.py")
    note = f" ({untranslatable} untranslatable CHECK(s) are listed in the report)" if untranslatable else ""
    return Check("PASS", "UNIQUE / foreign key / CHECK constraints are all in Postgres", "counts match" + note)


def check_sequences(pg_conn, oracle_names: list) -> Check:
    """After a migration the next generated id must be past the highest loaded id."""
    cur = pg_conn.cursor()
    bad, checked = [], 0
    for table in oracle_names:
        t = table.lower()
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
                    "AND table_name = %s", (t,))
        for (column,) in cur.fetchall():
            cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (f'"{t}"', column))
            sequence = cur.fetchone()[0]
            if not sequence:
                continue
            checked += 1
            cur.execute(f'SELECT MAX("{column}") FROM "{t}"')
            highest = cur.fetchone()[0]
            cur.execute(f"SELECT last_value, is_called FROM {sequence}")
            last, called = cur.fetchone()
            if highest is not None and (last + (1 if called else 0)) <= highest:
                bad.append(f"{t}.{column}: next id {last + (1 if called else 0)} but {highest} is already used")
    pg_conn.rollback()
    if bad:
        return Check("FAIL", "id sequences are ahead of the loaded data", "; ".join(bad), "run: python migrate_data.py")
    return Check("PASS", "id sequences are ahead of the loaded data", f"{checked} sequence(s)")


def check_functions(oracle_conn, pg_conn) -> Check:
    from read_schema import read_source_objects
    try:
        wanted = sorted({o["name"].lower() for o in read_source_objects()})
    except Exception as e:
        return Check("SKIP", "translated stored procedures are installed", _first_line(e))
    if not wanted:
        return Check("PASS", "translated stored procedures are installed", "the schema has none")
    cur = pg_conn.cursor()
    cur.execute("SELECT DISTINCT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = current_schema() AND p.prokind = 'f'")
    have = {r[0] for r in cur.fetchall()}
    pg_conn.rollback()
    missing = [n for n in wanted if n not in have]
    if missing:
        return Check("WARN", "translated stored procedures are installed",
                     f"{len(wanted) - len(missing)} of {len(wanted)}; not installed: {', '.join(missing)}",
                     "run: python plsql_translator.py --install   (functions that need manual work are never installed)")
    return Check("PASS", "translated stored procedures are installed", f"{len(wanted)} of {len(wanted)}")


def check_docker() -> Check:
    try:
        out = subprocess.run(["docker", "inspect", "-f", "{{.Name}} {{.HostConfig.RestartPolicy.Name}}",
                              "oracle-xe", "pg-target"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return Check("SKIP", "containers restart on their own", "the docker command is not available here")
    if out.returncode != 0:
        return Check("SKIP", "containers restart on their own", "could not inspect oracle-xe / pg-target")
    policies = dict(line.lstrip("/").split(" ", 1) for line in out.stdout.strip().splitlines() if " " in line)
    off = [name for name, policy in policies.items() if policy.strip() not in ("unless-stopped", "always")]
    if off:
        return Check("FAIL", "containers restart on their own", f"no restart policy on: {', '.join(off)}",
                     "run: docker update --restart unless-stopped oracle-xe pg-target")
    return Check("PASS", "containers restart on their own", ", ".join(f"{n}: {p}" for n, p in policies.items()))


# ------------------------------------------------------------ orchestration


def run_checks(skip_ai: bool = False, skip_docker: bool = False) -> list:
    results = [check_config()]
    config_ok = results[0].status == "PASS"

    results.append(Check("SKIP", "AI provider answers", "skipped by request") if skip_ai
                   else check_ai() if config_ok else Check("SKIP", "AI provider answers", "needs a working configuration"))

    oracle_conn = pg_conn = None
    if config_ok:
        try:
            oracle_conn = connect_oracle()
            results.append(Check("PASS", "Oracle answers", "SELECT 1 FROM dual"))
        except Exception as e:
            results.append(Check("FAIL", "Oracle answers", _first_line(e), "run: docker start oracle-xe   (Oracle takes a minute after a cold start)"))
        try:
            pg_conn = connect_postgres()
            results.append(Check("PASS", "Postgres answers", "connected"))
        except Exception as e:
            results.append(Check("FAIL", "Postgres answers", _first_line(e), "run: docker start pg-target"))
    else:
        results += [Check("SKIP", "Oracle answers", "needs a working configuration"),
                    Check("SKIP", "Postgres answers", "needs a working configuration")]

    names = None
    if oracle_conn:
        try:
            from read_schema import get_table_names
            names = get_table_names(oracle_conn.cursor())
            results.append(Check("PASS", "tables found in the Oracle schema", ", ".join(names)))
        except Exception as e:
            results.append(Check("FAIL", "tables found in the Oracle schema", _first_line(e),
                                 "check ORACLE_SCHEMA / ORACLE_TABLES in .env"))
    else:
        results.append(Check("SKIP", "tables found in the Oracle schema", "needs Oracle"))

    both = names is not None and pg_conn is not None
    for title, fn in (("every Oracle table exists in Postgres", lambda: check_tables_exist(names, pg_conn)),
                      ("row counts match", lambda: check_row_counts(oracle_conn, pg_conn, names)),
                      ("UNIQUE / foreign key / CHECK constraints are all in Postgres", lambda: check_constraints(oracle_conn, pg_conn, names)),
                      ("id sequences are ahead of the loaded data", lambda: check_sequences(pg_conn, names)),
                      ("translated stored procedures are installed", lambda: check_functions(oracle_conn, pg_conn))):
        if not both:
            results.append(Check("SKIP", title, "needs both databases"))
            continue
        try:
            results.append(fn())
        except Exception as e:
            results.append(Check("FAIL", title, f"the check itself failed: {_first_line(e)}"))

    results.append(Check("SKIP", "containers restart on their own", "skipped by request") if skip_docker else check_docker())

    for conn in (oracle_conn, pg_conn):
        try:
            conn and conn.close()
        except Exception:
            pass
    return results


def format_report(results: list) -> str:
    lines = []
    for r in results:
        lines.append(f"  {r.status:5} {r.title}" + (f"  -  {r.detail}" if r.detail else ""))
        if r.hint and r.status in ("FAIL", "WARN"):
            lines.append(f"         -> {r.hint}")
    counts = {s: sum(1 for r in results if r.status == s) for s in ("PASS", "WARN", "FAIL", "SKIP")}
    lines.append("")
    lines.append(f"  {counts['PASS']} passed, {counts['WARN']} to look at, {counts['FAIL']} failed, {counts['SKIP']} skipped")
    if not counts["FAIL"]:
        lines.append("  Everything that could be checked is in working order."
                     + ("  Next: python compare_procedures.py to compare the translations with the originals." if counts["PASS"] else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="One-command health check for the whole project.")
    parser.add_argument("--skip-ai", action="store_true")
    parser.add_argument("--skip-docker", action="store_true")
    args = parser.parse_args(argv)
    print("Checking the whole setup (nothing is changed):\n")
    results = run_checks(args.skip_ai, args.skip_docker)
    print(format_report(results))
    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
