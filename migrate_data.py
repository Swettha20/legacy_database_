"""
legacy-db-modernizer: Day 7-8 (updated: config.py, re-run safety, then: any schema)
Reads real row data out of Oracle in small batches and inserts it into
the matching PostgreSQL tables.

UPDATE (re-run safety): running this twice used to crash partway through
with a confusing UniqueViolation error, because it always tried to INSERT
rows with the original source ids - the second run collided with data
from the first. Fixed by TRUNCATEing each target table immediately before
migrating it (in the same transaction as the inserts, so a failure still
rolls back cleanly). This makes the script a full, safe re-migration each
time it's run - not an incremental/delta sync, which is a different,
larger feature intentionally out of scope here.

UPDATE (data guard): a fraction inserted into an INTEGER column is silently
rounded by Postgres. Each batch is now checked against the real target
column types before it is written; see check_whole_numbers().
"""

import decimal

import oracledb
import psycopg2
from config import (
    ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN, ORACLE_SCHEMA,
    PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME,
)
from build_ir import build_ir
from read_schema import read_schema

BATCH_SIZE = 500


def get_migration_plan() -> list:
    """
    The tables to load, in an order where parents come before the children
    that reference them, each with its column names. Derived from the live
    Oracle schema (the same discovery and ordering the schema step uses), so
    the loader can never disagree with the tables that were created.
    """
    ir = build_ir(read_schema())
    return [{"name": t["name"], "columns": [c["name"] for c in t["columns"]]}
            for t in ir["tables"]]


def get_oracle_connection():
    return oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN,
    )


def get_pg_connection():
    return psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        dbname=PG_DBNAME,
    )


class DataIntegrityError(RuntimeError):
    """The source data cannot be loaded into the target column without
    silently changing it. Raised BEFORE anything is written for that batch."""


def get_integer_columns(pg_cursor, table_name: str) -> set:
    """
    Columns the TARGET database actually holds as whole-number types. Read
    from Postgres itself (not from a JSON file), so it reflects exactly what
    the values are about to be inserted into.
    """
    pg_cursor.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
          AND data_type IN ('smallint', 'integer', 'bigint')
        """,
        (table_name,),
    )
    return {row[0] for row in pg_cursor.fetchall()}


def _is_whole_number(value) -> bool:
    if value is None or isinstance(value, int):
        return True
    if isinstance(value, float):
        return value.is_integer()          # 3.0 is fine; 2.5, NaN, inf are not
    if isinstance(value, decimal.Decimal):
        return value == value.to_integral_value()
    return True   # not numeric at all: let the database reject it, loudly


def check_whole_numbers(table_name: str, columns: list, integer_columns: set, batch: list):
    """
    Postgres silently ROUNDS a fraction inserted into an INTEGER column
    (2.5 is stored as 3, no error). That is data corruption that looks like
    success, and it happens when a column's type was a guess (the AI advisor
    picks it from the column NAME and never sees the data). So refuse to load
    a fraction into a whole-number column and say exactly where it is.
    """
    checks = [(i, c) for i, c in enumerate(columns) if c in integer_columns]
    if not checks:
        return
    id_index = columns.index("id") if "id" in columns else None
    for row in batch:
        for i, column in checks:
            if not _is_whole_number(row[i]):
                where = f" (row id {row[id_index]})" if id_index is not None else ""
                raise DataIntegrityError(
                    f"{table_name}.{column}: the source value {row[i]!r}{where} is not a "
                    f"whole number, but the column is a whole-number type in Postgres. "
                    f"Loading it would silently round it. Nothing was changed. "
                    f"This column's type needs to be NUMERIC (or the source data corrected)."
                )


def get_target_columns(pg_cursor, table_name: str) -> list:
    pg_cursor.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )
    return [row[0] for row in pg_cursor.fetchall()]


def truncate_tables(pg_cursor, table_names: list):
    """
    Clears exactly the tables about to be loaded, in one statement. No
    CASCADE on purpose: if some OTHER table references these, Postgres
    refuses (loudly) instead of silently wiping data we do not own.
    """
    names = ", ".join(f'"{t}"' for t in table_names)
    pg_cursor.execute(f"TRUNCATE TABLE {names} RESTART IDENTITY;")
    print("  Existing data cleared from all target tables (safe for re-running).")


def migrate_table(oracle_cursor, pg_cursor, table_name: str, columns: list):
    oracle_column_list = ", ".join(f'"{c.upper()}"' for c in columns)

    oracle_cursor.execute(
        f'SELECT {oracle_column_list} FROM "{ORACLE_SCHEMA}"."{table_name.upper()}"'
    )

    integer_columns = get_integer_columns(pg_cursor, table_name)

    total_rows = 0
    while True:
        batch = oracle_cursor.fetchmany(BATCH_SIZE)
        if not batch:
            break

        check_whole_numbers(table_name, columns, integer_columns, batch)

        placeholders = ", ".join(["%s"] * len(columns))
        column_list = ", ".join(f'"{c}"' for c in columns)
        insert_sql = (
            f'INSERT INTO "{table_name}" ({column_list}) VALUES ({placeholders})'
        )

        pg_cursor.executemany(insert_sql, batch)
        total_rows += len(batch)
        print(f"  ...wrote {len(batch)} rows (running total: {total_rows})")

    print(f"  {table_name}: {total_rows} rows migrated")
    return total_rows


def reset_pg_sequences(pg_cursor, table_name: str, columns: list):
    """
    For every column of this table that owns a sequence (SERIAL), move the
    sequence past the highest migrated value so the next normal INSERT does
    not collide. Works for any table: no assumption that the column is
    called "id", or that there is one at all. (Done AFTER the data commit:
    sequence changes are not rolled back with a transaction, so setting them
    inside it could leave a sequence out of step after a failed load.)
    """
    for column in columns:
        pg_cursor.execute("SELECT pg_get_serial_sequence(%s, %s)", (f'"{table_name}"', column))
        sequence = pg_cursor.fetchone()[0]
        if not sequence:
            continue
        pg_cursor.execute(
            f'''
            SELECT setval(
                %s,
                GREATEST(COALESCE((SELECT MAX("{column}") FROM "{table_name}"), 0), 1),
                EXISTS (SELECT 1 FROM "{table_name}")
            )
            ''',
            (sequence,),
        )


def migrate_all_data():
    plan = get_migration_plan()

    oracle_conn = get_oracle_connection()
    pg_conn = get_pg_connection()

    oracle_cursor = oracle_conn.cursor()
    pg_cursor = pg_conn.cursor()

    try:
        # Work out, per table, which columns exist on BOTH sides. A column
        # the schema step could not map (no type rule) is not in Postgres, so
        # it is left out here and reported instead of failing the whole load.
        loadable = []
        for entry in plan:
            target = get_target_columns(pg_cursor, entry["name"])
            if not target:
                raise RuntimeError(
                    f"Table '{entry['name']}' does not exist in Postgres - "
                    f"run the schema step before migrating data."
                )
            columns = [c for c in entry["columns"] if c in target]
            for skipped in (c for c in entry["columns"] if c not in target):
                print(f"  WARNING: {entry['name']}.{skipped} has no column in Postgres "
                      f"(unmapped type) - its data is NOT migrated")
            loadable.append((entry["name"], columns))

        truncate_tables(pg_cursor, [name for name, _ in loadable])

        # Self-referencing keys are DEFERRABLE: check them once at commit,
        # not row by row, so row order inside a table does not matter.
        pg_cursor.execute("SET CONSTRAINTS ALL DEFERRED")

        for table_name, columns in loadable:
            print(f"\n--- Migrating {table_name} ---")
            migrate_table(oracle_cursor, pg_cursor, table_name, columns)

        pg_conn.commit()

        for table_name, columns in loadable:
            reset_pg_sequences(pg_cursor, table_name, columns)
        pg_conn.commit()

        print("\nAll data migrated and sequences reset successfully.")

    except Exception:
        try:
            pg_conn.rollback()
        except Exception:
            # The connection is already dead (database stopped or dropped us
            # mid-load). Postgres discards an uncommitted transaction on its
            # own, so nothing is lost - but a failing rollback() here used to
            # replace the real error with "connection already closed".
            # Swallow only the rollback failure; re-raise the ORIGINAL error.
            pass
        raise
    finally:
        oracle_cursor.close()
        pg_cursor.close()
        oracle_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    migrate_all_data()
