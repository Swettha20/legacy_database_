"""
legacy-db-modernizer: converting an UPLOADED Oracle script

The migration reads real metadata from a live Oracle, so an uploaded .sql file
cannot be read directly. Instead:

  1. the script is run in a brand-new, throwaway Oracle schema (UPL_xxxxxxxx)
     whose user can only create objects in itself - never in SYSTEM or next to
     your other data. It is dropped again afterwards.
  2. the normal pipeline reads that schema and writes a new PostgreSQL
     database (upload_xxxxxxxx).
  3. everything is also written out as files: a PostgreSQL script that loads
     anywhere (schema + data + the translated procedures), the report, and the
     translated procedures one per file.

The pipeline reads the schema and database NAMES when its modules load, so
step 2 runs in a separate worker process started with those two settings
changed (python upload_pipeline.py --worker ...). The worker reports progress
as lines starting with @@EVENT@@, which the API turns into the live status.

Only run scripts you trust: they execute inside your local Oracle (with the
limited rights above).
"""

import json
import os
import re
import secrets
import sys
from dataclasses import dataclass, field

from oracle_script import split_script

EVENT_MARK = "@@EVENT@@ "
SCHEMA_PATTERN = re.compile(r"^UPL_[0-9A-F]{8,24}$")
GRANTS = ("CREATE SESSION, CREATE TABLE, CREATE VIEW, CREATE SEQUENCE, CREATE PROCEDURE, "
          "CREATE TRIGGER, CREATE TYPE, CREATE SYNONYM")
MAX_ERRORS_SHOWN = 10


class ScriptError(RuntimeError):
    """The uploaded script could not be turned into a usable Oracle schema.
    Carries what happened (a LoadResult) so the user can still see every statement that failed."""

    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


def scratch_schema_name(job_id: str) -> str:
    return "UPL_" + re.sub(r"[^0-9a-fA-F]", "", job_id)[:8].upper().ljust(8, "0")


def pg_database_name(job_id: str) -> str:
    return "upload_" + scratch_schema_name(job_id)[4:].lower()


# ----------------------------------------------------------------- Oracle side


@dataclass
class LoadResult:
    executed: int = 0
    failed: list = field(default_factory=list)          # [{"line", "kind", "message"}]
    skipped: list = field(default_factory=list)         # SQL*Plus commands that were not sent
    tables: int = 0
    compile_errors: list = field(default_factory=list)


def _first_line(text) -> str:
    return (str(text).strip().splitlines() or [""])[0][:300]


def create_scratch_schema(admin_connection, schema: str, password: str):
    if not SCHEMA_PATTERN.match(schema):
        raise ValueError(f"refusing to create schema {schema!r}")
    if not re.fullmatch(r"[0-9a-f]{16,64}", password):
        raise ValueError("unsafe password")
    cur = admin_connection.cursor()
    try:
        cur.execute(f'CREATE USER {schema} IDENTIFIED BY "{password}" QUOTA UNLIMITED ON USERS')
        cur.execute(f"GRANT {GRANTS} TO {schema}")
        cur.execute(f"GRANT EXECUTE ON SYS.DBMS_FLASHBACK TO {schema}")
    finally:
        cur.close()


def drop_scratch_schema(admin_connection, schema: str):
    if not SCHEMA_PATTERN.match(schema):
        raise ValueError(f"refusing to drop schema {schema!r}")
    cur = admin_connection.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM all_users WHERE username = :u", u=schema)
        if cur.fetchone()[0]:
            cur.execute(f"DROP USER {schema} CASCADE")
    finally:
        cur.close()


def run_script(user_connection, script_text: str, progress=None, strict=True) -> LoadResult:
    """
    Runs every statement of the script, continuing after a failure (as SQL*Plus
    does: a DROP of something that does not exist should not stop a script), and
    records what failed. Raises ScriptError only if the script created no table.
    """
    statements, skipped = split_script(script_text)
    result = LoadResult(skipped=skipped)
    if strict and any(re.match(r'(?i)^\s*(@|START\b|CONNECT\b|CONN\b|DEFINE\b|VARIABLE\b|VAR\b|HOST\b)', line) for line in skipped):
        raise ScriptError('This script depends on SQLcl/SQL*Plus includes, variables or connections. Use its official installer and migrate the installed schema.',result)
    if not statements:
        raise ScriptError("The file contains no SQL statements.")
    cur = user_connection.cursor()
    try:
        for index, statement in enumerate(statements, 1):
            try:
                cur.execute(statement.sql)
                result.executed += 1
            except Exception as e:                                   # one bad statement must not stop the rest
                result.failed.append({"line": statement.line, "kind": statement.kind, "message": _first_line(e)})
                if strict:
                    user_connection.rollback()
                    raise ScriptError(f"Stopped at line {statement.line}: {_first_line(e)}", result) from e
            if progress and (index % 200 == 0 or index == len(statements)):
                progress(index, len(statements))
        user_connection.commit()
        cur.execute("SELECT COUNT(*) FROM user_tables")
        result.tables = cur.fetchone()[0]
        cur.execute("SELECT name, line, text FROM user_errors ORDER BY name, sequence")
        result.compile_errors = [f"{n} line {l}: {_first_line(t)}" for n, l, t in cur.fetchall()]
        if strict and result.compile_errors:
            raise ScriptError("Oracle compilation failed: " + result.compile_errors[0], result)
    finally:
        cur.close()
    if result.tables == 0:
        shown = "; ".join(f"line {f['line']}: {f['message']}" for f in result.failed[:3]) or "no CREATE TABLE statement was found"
        raise ScriptError(f"Oracle created no tables from this script ({shown}).", result)
    return result


# ------------------------------------------------------------ PostgreSQL side


def create_pg_database(name: str):
    import psycopg2
    from config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD
    if not re.fullmatch(r"upload_[0-9a-f]{8,33}", name):
        raise ValueError(f"refusing to create database {name!r}")
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="postgres")
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(f'CREATE DATABASE "{name}" ENCODING \'UTF8\' TEMPLATE template0')
    finally:
        conn.close()


def drop_pg_database(name: str):
    import psycopg2
    from config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD
    if not re.fullmatch(r"upload_[0-9a-f]{8,33}", name):
        raise ValueError(f"refusing to drop database {name!r}")
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="postgres")
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        conn.close()


def export_dump(pg_conn, mapped: dict, function_sql: list, path: str):
    """
    Writes a standalone PostgreSQL script: tables, data, id counters and the
    translated functions. Load it anywhere with:  psql -d yourdb -f file.sql
    """
    from write_postgres_schema import build_create_table_sql
    tables = mapped["tables"]
    cur = pg_conn.cursor()
    with open(path, "w", encoding="utf-8", newline="\n") as out:
        out.write("-- Converted from an Oracle script by legacy-db-modernizer\n"
                  "-- Load with:  psql -U postgres -d your_database -f this_file.sql\n"
                  "-- It drops and recreates these tables, so use an empty or scratch database.\n\n"
                  "SET client_encoding = 'UTF8';\nBEGIN;\nSET CONSTRAINTS ALL DEFERRED;\n\n")
        for table in reversed(tables):
            out.write(f'DROP TABLE IF EXISTS "{table["name"]}" CASCADE;\n')
        out.write("\n")
        for table in tables:
            out.write(build_create_table_sql(table) + "\n\n")
        for table in tables:
            columns = [c["name"] for c in table["columns"] if c["pg_type"] is not None]
            column_list = ", ".join(f'"{c}"' for c in columns)
            out.write(f'COPY "{table["name"]}" ({column_list}) FROM stdin;\n')
            cur.copy_expert(f'COPY "{table["name"]}" ({column_list}) TO STDOUT', out)
            out.write("\\.\n\n")
        for table in tables:
            for column in (c["name"] for c in table["columns"] if c["pg_type"] is not None):
                cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (f'"{table["name"]}"', column))
                if cur.fetchone()[0]:
                    t = f'"{table["name"]}"'
                    out.write(f"SELECT setval(pg_get_serial_sequence('{t}', '{column}'), "
                              f'GREATEST(COALESCE((SELECT MAX("{column}") FROM {t}), 0), 1), '
                              f"EXISTS (SELECT 1 FROM {t}));\n")
        from moderniser.objects import statements
        for statement in statements(mapped):
            out.write('\n'+statement+'\n')
        for sql in function_sql:
            out.write("\n" + sql.strip().rstrip(";") + ";\n")
        out.write("\nCOMMIT;\n")
    pg_conn.rollback()


# ------------------------------------------------------------------- the worker


def make_emitter(stream=None):
    stream = stream or sys.stdout

    def emit(**fields):
        stream.write(EVENT_MARK + json.dumps(fields, default=str) + "\n")
        stream.flush()
    return emit


def translate_procedures(job_dir: str, emit) -> list:
    """Translates and installs each stored procedure; returns the SQL of those installed."""
    from llm_provider import LLMConfigError
    from plsql_translator import (TranslationUnavailable, _result_file_text, install_in_postgres,
                                  translate_procedure_detailed)
    from read_schema import read_source_objects
    installed_sql = []
    out_dir = os.path.join(job_dir, "translated")
    os.makedirs(out_dir, exist_ok=True)
    for obj in read_source_objects():
        emit(event="procedure_start", name=obj["name"])
        try:
            result = translate_procedure_detailed(obj["source"], obj["name"], obj["type"].lower())
        except (TranslationUnavailable, LLMConfigError) as e:
            emit(event="warning", message=f"Stored procedures were not translated: {e}")
            break
        safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', obj['name']).lower()
        with open(os.path.join(out_dir, safe_name + ".sql"), "w", encoding="utf-8") as f:
            f.write(_result_file_text(result))
        installed = False
        if result.ok and result.pg_checked and not result.manual:
            installed, _ = install_in_postgres(result.sql)
            if installed:
                installed_sql.append(result.sql)
        status = "NEEDS MANUAL WORK" if result.manual else ("OK" if result.ok else "NEEDS FIXING")
        emit(event="procedure_done", name=obj["name"], status=status, attempts=result.attempts,
             installed=installed, notes=len(result.advisories))
    return installed_sql


def run_pipeline(job_dir: str, translate: bool, emit):
    """The worker's job. Runs with ORACLE_SCHEMA and PG_DBNAME already pointing at the upload."""
    from build_ir import build_ir
    from migrate_data import get_pg_connection, migrate_all_data
    from read_schema import read_schema, read_unmigrated_objects
    from type_mapper import map_schema
    from write_postgres_schema import get_pg_connection_for_api, write_schema_to_postgres

    os.makedirs(job_dir, exist_ok=True)
    emit(event="step", key="reading_schema")
    oracle_schema = read_schema()
    emit(event="step", key="building_ir")
    ir = build_ir(oracle_schema, read_unmigrated_objects())
    emit(event="step", key="mapping_types")
    mapped = map_schema(ir)
    with open(os.path.join(job_dir, "mapped_schema.json"), "w", encoding="utf-8") as f:
        json.dump(mapped, f, indent=1, default=str)
    emit(event="step", key="writing_postgres_schema")
    conn = get_pg_connection_for_api()
    try:
        write_schema_to_postgres(mapped, conn)
    finally:
        conn.close()
    emit(event="step", key="migrating_data")
    summary = migrate_all_data(progress=lambda e: emit(**e))
    functions = []
    if translate:
        emit(event="step", key="translating_procedures")
        functions = translate_procedures(job_dir, emit)
    emit(event="step", key="exporting_files")
    conn = get_pg_connection()
    try:
        export_dump(conn, mapped, functions, os.path.join(job_dir, "converted_postgresql.sql"))
    finally:
        conn.close()
    emit(event="done", summary=summary)


def worker_main(argv) -> int:
    emit = make_emitter()
    job_dir, translate = argv[0], "--translate" in argv
    try:
        run_pipeline(job_dir, translate, emit)
        return 0
    except Exception as e:
        emit(event="error", message=str(e).strip() or type(e).__name__)
        return 1


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--worker":
        sys.exit(worker_main(sys.argv[2:]))
    print("This module is used by the API. See the README, 'Converting an uploaded Oracle script'.")
