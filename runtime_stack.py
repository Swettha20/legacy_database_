"""Container bootstrap and health checks. Never migrate or replace target data."""
import argparse
import os
from pathlib import Path
import sys


def sample_state(tables):
    """Seed only a genuinely empty schema, never overwrite an existing one."""
    wanted = {"BOOKS", "MEMBERS", "LOANS"}
    tables = {t.upper() for t in tables}
    if not tables:
        return "empty"
    if wanted <= tables:
        return "ready"
    return "partial"


def seed():
    import oracledb
    from config import ORACLE_DSN
    from upload_pipeline import run_script

    password = os.environ.get("DEMO_PASSWORD")
    if not password:
        raise RuntimeError("DEMO_PASSWORD is missing; run this via start_all / Compose.")
    with oracledb.connect(user="DEMO", password=password, dsn=ORACLE_DSN,
                          tcp_connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT table_name FROM user_tables")
            state = sample_state([r[0] for r in cur])
        if state == "ready":
            print("PASS  Existing demo tables retained; no records changed.", flush=True)
            return
        if state != "empty":
            raise RuntimeError("DEMO contains an incomplete or different schema. Nothing was replaced. "
                               "Inspect it before retrying; do not delete data to silence this check.")
        result = run_script(conn, Path("sample_schema.sql").read_text(encoding="utf-8"))
        if result.failed or result.compile_errors:
            raise RuntimeError(f"Sample initialization failed: {result.failed}; {result.compile_errors}")
        print("PASS  Created the library sample in the isolated DEMO schema.", flush=True)


def database_checks():
    """Readiness is about connectivity, not whether a user has migrated yet."""
    import oracledb
    import psycopg2
    from config import (ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN,
                        PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME)

    checks = []
    for name, connect, sql in (
        ("Oracle", lambda: oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD,
                                            dsn=ORACLE_DSN, tcp_connect_timeout=5), "SELECT 1 FROM dual"),
        ("PostgreSQL", lambda: psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
                                               password=PG_PASSWORD, dbname=PG_DBNAME,
                                               connect_timeout=5), "SELECT 1"),
    ):
        try:
            conn = connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    if cur.fetchone()[0] != 1:
                        raise RuntimeError("Unexpected probe result")
            finally:
                conn.close()
            checks.append({"service": name, "status": "PASS"})
        except Exception:
            # Credentials / raw connection errors must not leak through HTTP.
            checks.append({"service": name, "status": "FAIL"})
    return checks


def health(skip_ai=False):
    skip_ai = skip_ai or os.getenv('LLM_PROVIDER') == 'none'
    from check_setup import (Check, check_config, check_ai, connect_oracle, connect_postgres,
                             check_encoding, check_tables_exist, check_row_counts,
                             check_constraints, check_sequences, check_functions, format_report)
    from config import ORACLE_SCHEMA
    from read_schema import get_table_names

    results = [check_config()]
    connections = {}
    for label, connect in (("Oracle", connect_oracle), ("PostgreSQL", connect_postgres)):
        try:
            connections[label] = connect()
            results.append(Check("PASS", f"{label} accepts authenticated queries"))
        except Exception:
            results.append(Check("FAIL", f"{label} accepts authenticated queries",
                                 "connection/query failed; inspect container logs and .env.stack"))
    try:
        if len(connections) == 2:
            oracle, pg = connections["Oracle"], connections["PostgreSQL"]
            tables = get_table_names(oracle.cursor())
            if not tables:
                results.append(Check("FAIL", "Oracle sample tables", "no tables found"))
            else:
                results.append(Check("PASS", f"Oracle schema {ORACLE_SCHEMA}", ", ".join(tables)))
            results.append(check_encoding(oracle, pg))
            from moderniser.store import store
            with store.connection() as metadata, metadata.cursor() as cursor:
                cursor.execute("SELECT count(*) n FROM records WHERE kind='job'")
                count=cursor.fetchone()['n']
            results.append(Check('PASS','durable job metadata',f'{count} saved jobs'))
            completed=[job for job in store.list('job',limit=100) if job['status'] in ('completed','completed_with_warnings') and not job.get('deleted')]
            if completed:
                latest=completed[0]
                verified=latest.get('verification',{}).get('passed') and latest.get('verification',{}).get('export_restore',{}).get('passed')
                results.append(Check('PASS' if verified else 'WARN','latest completed job evidence',
                    f"{latest['id'][:8]}: saved data/export verification {'passed' if verified else 'requires review'}; see its report"))
            else:
                results.append(Check('SKIP','migration verification','No completed job yet. Inspect, review and approve a migration on the website.'))
        if skip_ai:
            results.append(Check("SKIP", "AI inference", "explicitly skipped; translation readiness is not verified"))
        else:
            results.append(check_ai())
    finally:
        for conn in connections.values():
            conn.close()
    print(format_report(results), flush=True)
    return 1 if any(r.status == "FAIL" for r in results) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["seed", "health"])
    parser.add_argument("--skip-ai", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "seed":
        from moderniser.provision import provision, grant_reader
        provision()
        seed()
        grant_reader()
        return 0
    return health(args.skip_ai)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"FAIL  {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
