"""
legacy-db-modernizer: Day 2
Connects to the Oracle sample schema and reads its metadata:
tables -> columns (name, type, length, nullable, default) -> primary/foreign keys.

This is READ-ONLY. It doesn't build the IR yet (that's Day 3) -
it just proves we can pull structured metadata out of Oracle reliably,
and prints it in a readable form so we can eyeball it's correct
before we start transforming it.
"""

import oracledb

# Same connection details as test_connection.py -- keep these in one
# place once we wrap this in FastAPI (Day 9); for now, duplicated is fine.
connection = oracledb.connect(
    user="system",
    password="YourPassword123",
    dsn="localhost:1521/XEPDB1"
)

# We only care about the tables *we* created, not Oracle's own SYSTEM
# tables (there are hundreds of those). List them explicitly for now;
# Day 3+ can switch this to "every table owned by a given app schema"
# once we're not sharing the SYSTEM user for our own tables.
OUR_TABLES = ["MEMBERS", "BOOKS", "LOANS"]


def get_columns(cursor, table_name):
    cursor.execute(
        """
        SELECT column_name, data_type, data_length, nullable, data_default
        FROM all_tab_columns
        WHERE table_name = :table_name
          AND owner = 'SYSTEM'
        ORDER BY column_id
        """,
        table_name=table_name,
    )
    columns = []
    for col_name, data_type, data_length, nullable, default in cursor.fetchall():
        columns.append({
            "name": col_name,
            "type": data_type,
            "length": data_length,
            "nullable": nullable == "Y",
            "default": default.strip() if default else None,
        })
    return columns


def get_primary_key(cursor, table_name):
    cursor.execute(
        """
        SELECT acc.column_name
        FROM all_constraints ac
        JOIN all_cons_columns acc
          ON ac.constraint_name = acc.constraint_name
         AND ac.owner = acc.owner
        WHERE ac.table_name = :table_name
          AND ac.owner = 'SYSTEM'
          AND ac.constraint_type = 'P'
        ORDER BY acc.position
        """,
        table_name=table_name,
    )
    return [row[0] for row in cursor.fetchall()]


def get_foreign_keys(cursor, table_name):
    cursor.execute(
        """
        SELECT acc.column_name, r_ac.table_name AS ref_table, r_acc.column_name AS ref_column
        FROM all_constraints ac
        JOIN all_cons_columns acc
          ON ac.constraint_name = acc.constraint_name
         AND ac.owner = acc.owner
        JOIN all_constraints r_ac
          ON ac.r_constraint_name = r_ac.constraint_name
         AND ac.r_owner = r_ac.owner
        JOIN all_cons_columns r_acc
          ON r_ac.constraint_name = r_acc.constraint_name
         AND r_ac.owner = r_acc.owner
         AND acc.position = r_acc.position
        WHERE ac.table_name = :table_name
          AND ac.owner = 'SYSTEM'
          AND ac.constraint_type = 'R'
        """,
        table_name=table_name,
    )
    foreign_keys = []
    for column, ref_table, ref_column in cursor.fetchall():
        foreign_keys.append({
            "column": column,
            "references_table": ref_table,
            "references_column": ref_column,
        })
    return foreign_keys


def read_schema():
    cursor = connection.cursor()
    schema = {}

    for table_name in OUR_TABLES:
        schema[table_name] = {
            "columns": get_columns(cursor, table_name),
            "primary_key": get_primary_key(cursor, table_name),
            "foreign_keys": get_foreign_keys(cursor, table_name),
        }

    cursor.close()
    return schema


def print_schema(schema):
    for table_name, details in schema.items():
        print(f"\n=== {table_name} ===")
        print(f"Primary key: {details['primary_key']}")

        print("Columns:")
        for col in details["columns"]:
            nullable = "NULL" if col["nullable"] else "NOT NULL"
            default = f" DEFAULT {col['default']}" if col["default"] else ""
            print(f"  - {col['name']}: {col['type']}({col['length']}) {nullable}{default}")

        if details["foreign_keys"]:
            print("Foreign keys:")
            for fk in details["foreign_keys"]:
                print(f"  - {fk['column']} -> {fk['references_table']}.{fk['references_column']}")


if __name__ == "__main__":
    schema = read_schema()
    print_schema(schema)
    connection.close()
