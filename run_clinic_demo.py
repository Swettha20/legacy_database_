"""
legacy-db-modernizer: the second sample, end to end, in one command

    python run_clinic_demo.py          # run everything
    python run_clinic_demo.py --drop   # remove the clinic sample (Oracle schema + Postgres database)

Takes a different database from the library sample (a medical clinic, in its
own Oracle schema CLINIC) through the SAME pipeline, into its own Postgres
database (clinic_db), so it never touches the library sample's data:

    create it in Oracle -> read the schema -> map types -> create the tables ->
    copy the data -> translate and install the stored procedures -> compare each
    procedure with its Oracle original -> health check

Two settings are overridden for the child steps only, so your .env is not
changed:  ORACLE_SCHEMA=CLINIC  and  PG_DBNAME=clinic_db.
"""

import argparse
import os
import subprocess
import sys
import time

CLINIC_ENV = {"ORACLE_SCHEMA": "CLINIC", "PG_DBNAME": "clinic_db"}
PY = sys.executable

# (title, command or callable, show its output, stop here if it fails)
STEPS = [
    ("Create the clinic sample in Oracle (schema CLINIC)", [PY, "load_clinic_sample.py"], True, True),
    ("Create the Postgres database clinic_db", "create_pg_database", False, True),
    ("Read the Oracle schema and build the intermediate representation", [PY, "build_ir.py"], False, True),
    ("Map Oracle types to PostgreSQL types", [PY, "type_mapper.py"], False, True),
    ("Create the PostgreSQL tables", [PY, "write_postgres_schema.py"], False, True),
    ("Copy the data", [PY, "migrate_data.py"], False, True),
    ("Translate and install the stored procedures", [PY, "plsql_translator.py", "--install", "--out", "translated_clinic"], True, True),
    ("Compare each procedure with its Oracle original", [PY, "compare_procedures.py", "--tests", "procedure_tests_clinic.json"], True, False),
    ("Health check", [PY, "check_setup.py", "--skip-docker"], True, False),
]


def child_env() -> dict:
    env = dict(os.environ)
    env.pop("ORACLE_TABLES", None)          # discover the clinic's tables, do not inherit a library list
    env.update(CLINIC_ENV)
    return env


def create_pg_database(drop: bool = False) -> str:
    import psycopg2
    from config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="postgres")
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (CLINIC_ENV["PG_DBNAME"],))
        exists = cur.fetchone() is not None
        if drop:
            if exists:
                cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                            (CLINIC_ENV["PG_DBNAME"],))
                cur.execute(f'DROP DATABASE "{CLINIC_ENV["PG_DBNAME"]}"')
                return "dropped"
            return "did not exist"
        if exists:
            return "already exists"
        cur.execute(f'CREATE DATABASE "{CLINIC_ENV["PG_DBNAME"]}" ENCODING \'UTF8\' TEMPLATE template0')
        return "created"
    finally:
        cur.close()
        conn.close()


def run_step(command, show: bool, run=subprocess.run):
    """(ok, output). Output is shown only when asked for, or when the step fails."""
    if command == "create_pg_database":
        try:
            return True, create_pg_database()
        except Exception as e:
            return False, str(e)
    done = run(command, env=child_env(), capture_output=True, text=True)
    return done.returncode == 0, (done.stdout or "") + (done.stderr or "")


def main(argv=None, run=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description="Run the second (clinic) sample end to end.")
    parser.add_argument("--drop", action="store_true", help="remove the clinic sample again")
    args = parser.parse_args(argv)

    if args.drop:
        print("Removing the clinic sample:")
        done = run([PY, "load_clinic_sample.py", "--drop"], env=child_env(), capture_output=True, text=True)
        print(done.stdout.rstrip() or "  (nothing printed)")
        print(f"  Postgres database clinic_db: {create_pg_database(drop=True)}")
        return 0 if done.returncode == 0 else 1

    print("Running the clinic sample through the whole pipeline (Oracle schema CLINIC -> Postgres clinic_db):\n")
    failed_soft = []
    for number, (title, command, show, hard) in enumerate(STEPS, 1):
        print(f"{number}. {title}")
        started = time.time()
        ok, output = run_step(command, show, run)
        took = f"{time.time() - started:.1f}s"
        if ok:
            print(f"   ✓ done ({took})")
            if show and output.strip():
                print("\n".join("   " + line for line in output.rstrip().splitlines()))
        else:
            print(f"   ✗ FAILED ({took})")
            print("\n".join("   " + line for line in output.rstrip().splitlines()[-25:]))
            if hard:
                print("\nStopped here: the later steps need this one. Fix the problem above and run the command again "
                      "(it is safe to repeat).")
                return 1
            failed_soft.append(title)
    print("\n" + ("Everything in the clinic sample ran and checked out." if not failed_soft else
                  "Finished, but these steps reported problems: " + "; ".join(failed_soft)))
    print("Clean up with:  python run_clinic_demo.py --drop")
    return 1 if failed_soft else 0


if __name__ == "__main__":
    sys.exit(main())
