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
"""

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


def truncate_tables(pg_cursor):
    for table_name in reversed(TABLES_IN_ORDER):
        pg_cursor.execute(f'TRUNCATE TABLE "{table_name}" RESTART IDENTITY CASCADE;')
    print("  Existing data cleared from all target tables (safe for re-running).")


def migrate_table(oracle_cursor, pg_cursor, table_name: str):
    columns = TABLE_COLUMNS[table_name]
    oracle_column_list = ", ".join(columns)

    oracle_cursor.execute(f"SELECT {oracle_column_list} FROM {table_name.upper()}")

    total_rows = 0
    while True:
        batch = oracle_cursor.fetchmany(BATCH_SIZE)
        if not batch:
            break

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
