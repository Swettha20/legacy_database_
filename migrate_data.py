"""
legacy-db-modernizer: Day 7-8 (updated: config.py, then: re-run safety)
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
    ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN,
    PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME,
)

BATCH_SIZE = 500

TABLES_IN_ORDER = ["members", "books", "loans"]

TABLE_COLUMNS = {
    "members": ["id", "name", "email", "join_date"],
    "books": ["id", "title", "author", "isbn", "total_copies", "available_copies"],
    "loans": ["id", "member_id", "book_id", "loan_date", "due_date", "return_date"],
}


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


def truncate_tables(pg_cursor):
    for table_name in reversed(TABLES_IN_ORDER):
        pg_cursor.execute(f'TRUNCATE TABLE "{table_name}" RESTART IDENTITY CASCADE;')
    print("  Existing data cleared from all target tables (safe for re-running).")


def migrate_table(oracle_cursor, pg_cursor, table_name: str):
    columns = TABLE_COLUMNS[table_name]
    oracle_column_list = ", ".join(columns)

    oracle_cursor.execute(f"SELECT {oracle_column_list} FROM {table_name.upper()}")

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


def reset_pg_sequence(pg_cursor, table_name: str):
    pg_cursor.execute(f"""
        SELECT setval(
            pg_get_serial_sequence('"{table_name}"', 'id'),
            COALESCE((SELECT MAX(id) FROM "{table_name}"), 1)
        )
    """)


def migrate_all_data():
    oracle_conn = get_oracle_connection()
    pg_conn = get_pg_connection()

    oracle_cursor = oracle_conn.cursor()
    pg_cursor = pg_conn.cursor()

    try:
        truncate_tables(pg_cursor)

        for table_name in TABLES_IN_ORDER:
            print(f"\n--- Migrating {table_name} ---")
            migrate_table(oracle_cursor, pg_cursor, table_name)

        pg_conn.commit()

        for table_name in TABLES_IN_ORDER:
            reset_pg_sequence(pg_cursor, table_name)
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
