"""
legacy-db-modernizer: Day 7-8
Reads real row data out of Oracle in small batches and inserts it into
the matching PostgreSQL tables created on Day 6.

Batches, not load-the-whole-table: even though our sample data is tiny,
this proves the pattern that would let the same code handle a real,
much larger table without changes - fetch a batch, write it, discard it,
fetch the next batch. Memory use stays flat regardless of table size.

ID handling: both members.id and books.id use Oracle's GENERATED ALWAYS
AS IDENTITY, and the Postgres side uses SERIAL - two independent
sequences. To keep foreign key relationships valid (loans.member_id ->
members.id), we explicitly copy the SOURCE ids across rather than
letting Postgres generate new ones, then reset Postgres's sequence
afterward so future inserts (Day 9+ onward) don't collide with the
copied ids.
"""

import oracledb
import psycopg2

BATCH_SIZE = 500

# Order matters: migrate parent tables (members, books) before the
# child table (loans) that references them via foreign keys.
TABLES_IN_ORDER = ["members", "books", "loans"]

TABLE_COLUMNS = {
    "members": ["id", "name", "email", "join_date"],
    "books": ["id", "title", "author", "isbn", "total_copies", "available_copies"],
    "loans": ["id", "member_id", "book_id", "loan_date", "due_date", "return_date"],
}


def get_oracle_connection():
    return oracledb.connect(
        user="system",
        password="YourPassword123",  # match your Oracle container password
        dsn="localhost:1521/XEPDB1",
    )


def get_pg_connection():
    return psycopg2.connect(
        host="localhost",
        port=5432,
        user="postgres",
        password="YourPgPassword123",  # match your Postgres container password
        dbname="modernized_db",
    )


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
    """
    After explicitly inserting our own id values, Postgres's SERIAL
    sequence doesn't know about them yet - it would try to hand out
    id=1 again on the next auto-generated insert, colliding with data
    we just wrote. This advances the sequence to match the highest id
    actually present in the table.
    """
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
        for table_name in TABLES_IN_ORDER:
            print(f"\n--- Migrating {table_name} ---")
            migrate_table(oracle_cursor, pg_cursor, table_name)

        pg_conn.commit()

        # Reset sequences only after all data is committed
        for table_name in TABLES_IN_ORDER:
            reset_pg_sequence(pg_cursor, table_name)
        pg_conn.commit()

        print("\nAll data migrated and sequences reset successfully.")

    except Exception:
        pg_conn.rollback()
        raise
    finally:
        oracle_cursor.close()
        pg_cursor.close()
        oracle_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    migrate_all_data()