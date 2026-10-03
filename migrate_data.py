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

UPDATE (volume): rows are now written with PostgreSQL COPY (about 8x faster than
row-by-row INSERT, measured), with a fallback for unfamiliar value types, a row-count
check per table, throttled progress output, and ANALYZE after loading.

UPDATE (data guard): a fraction inserted into an INTEGER column is silently
rounded by Postgres. Each batch is now checked against the real target
column types before it is written; see check_whole_numbers().
"""

import datetime
import decimal
import io
import time

import oracledb
import psycopg2
import psycopg2.extras
from config import (
    ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN, ORACLE_SCHEMA,
    PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME,
)
from build_ir import build_ir
from read_schema import read_schema

BATCH_SIZE = 5000
PROGRESS_EVERY_ROWS = 100_000


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


def _lobs_as_plain_values(cursor, metadata):
    """
    By default the Oracle driver hands back CLOB/BLOB columns as LOB handles,
    which cannot be inserted into Postgres. Ask for ordinary str / bytes
    instead (the driver's documented way; fine for LOBs that fit in memory).
    """
    code = metadata.type_code
    if code in (oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_NCLOB):
        return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
    if code == oracledb.DB_TYPE_BLOB:
        return cursor.var(oracledb.DB_TYPE_LONG_RAW, arraysize=cursor.arraysize)
    return None


def get_oracle_connection():
    connection = oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN,
    )
    connection.outputtypehandler = _lobs_as_plain_values
    return connection


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


# Python value types whose text form is known to load into Postgres exactly as
# the normal INSERT path would. Anything else (an interval, a driver object we
# have not seen) takes the slower, driver-adapted path instead of risking it.
_COPY_SAFE_TYPES = (type(None), int, float, str, bytes, bytearray, memoryview,
                    decimal.Decimal, datetime.datetime, datetime.date)


def _copy_field(value) -> str:
    """One value as a COPY ... CSV field. NULL is the unquoted marker \\N; every
    real value is quoted, so an empty string, the text "\\N" and values with
    commas, quotes or newlines are all kept apart from NULL and from each other."""
    if value is None:
        return r"\N"
    if isinstance(value, (bytes, bytearray, memoryview)):
        text = r"\x" + bytes(value).hex()
    elif isinstance(value, float):
        if value != value:
            text = "NaN"
        elif value == float("inf"):
            text = "Infinity"
        elif value == float("-inf"):
            text = "-Infinity"
        else:
            text = repr(value)
    elif isinstance(value, (datetime.datetime, datetime.date)):
        text = value.isoformat()
    else:
        text = str(value)
    return '"' + text.replace('"', '""') + '"'


def _insert_batch(pg_cursor, table_name: str, columns: list, batch: list):
    """The conservative path: one multi-row INSERT per page, values adapted by the driver."""
    column_list = ", ".join(f'"{c}"' for c in columns)
    psycopg2.extras.execute_values(
        pg_cursor, f'INSERT INTO "{table_name}" ({column_list}) VALUES %s', batch, page_size=len(batch)
    )


def _copy_batch(pg_cursor, table_name: str, columns: list, batch: list):
    """The fast path: PostgreSQL COPY, about 8x faster than row-by-row INSERT."""
    column_list = ", ".join(f'"{c}"' for c in columns)
    text = "".join(",".join(_copy_field(v) for v in row) + "\n" for row in batch)
    pg_cursor.copy_expert(
        f"COPY \"{table_name}\" ({column_list}) FROM STDIN WITH (FORMAT csv, NULL '\\N')",
        io.StringIO(text),
    )


def load_batch(pg_cursor, table_name: str, columns: list, batch: list):
    seen = {type(v) for row in batch for v in row}
    if all(issubclass(t, _COPY_SAFE_TYPES) for t in seen):
        _copy_batch(pg_cursor, table_name, columns, batch)
    else:
        _insert_batch(pg_cursor, table_name, columns, batch)


def migrate_table(oracle_cursor, pg_cursor, table_name: str, columns: list):
    oracle_column_list = ", ".join(f'"{c.upper()}"' for c in columns)

    # One network round trip per batch instead of one per 100 rows (the driver default).
    oracle_cursor.arraysize = BATCH_SIZE
    oracle_cursor.execute(
        f'SELECT {oracle_column_list} FROM "{ORACLE_SCHEMA}"."{table_name.upper()}"'
    )

    integer_columns = get_integer_columns(pg_cursor, table_name)

    started = time.time()
    total_rows = 0
    next_progress = PROGRESS_EVERY_ROWS
    while True:
        batch = oracle_cursor.fetchmany(BATCH_SIZE)
        if not batch:
            break

        check_whole_numbers(table_name, columns, integer_columns, batch)
        load_batch(pg_cursor, table_name, columns, batch)
        total_rows += len(batch)

        if total_rows >= next_progress:      # a line per batch would be thousands of lines
            rate = total_rows / max(time.time() - started, 1e-9)
            print(f"  ...{table_name}: {total_rows:,} rows so far ({rate:,.0f} rows/s)")
            next_progress += PROGRESS_EVERY_ROWS

    # The table was emptied at the start of this transaction, so the target
    # must now hold exactly the rows we sent - catch a short write now, while
    # a rollback still undoes everything.
    pg_cursor.execute(f'SELECT count(*) FROM "{table_name}"')
    in_target = pg_cursor.fetchone()[0]
    if in_target != total_rows:
        raise RuntimeError(
            f"{table_name}: {total_rows:,} rows were read from Oracle but Postgres holds "
            f"{in_target:,}. Nothing was committed."
        )

    elapsed = time.time() - started
    print(f"  {table_name}: {total_rows:,} rows migrated ({elapsed:.1f}s)")
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

        # A freshly bulk-loaded table has no planner statistics until
        # autovacuum gets round to it; queries would be badly planned meanwhile.
        for table_name, _ in loadable:
            try:
                pg_cursor.execute(f'ANALYZE "{table_name}"')
            except Exception as e:      # the data is already safely committed
                print(f"  WARNING: ANALYZE {table_name} failed ({e}); run it manually")
                pg_conn.rollback()
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
