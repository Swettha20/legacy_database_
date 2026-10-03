"""
legacy-db-modernizer: Day 2 (fixed after Day 9 caught a real bug)
Connects to the Oracle sample schema and reads its metadata:
tables -> columns (name, type, length, nullable, default) -> primary/foreign keys.

This is READ-ONLY. It doesn't build the IR yet (that's Day 3) -
it just proves we can pull structured metadata out of Oracle reliably,
and prints it in a readable form so we can eyeball it's correct
before we start transforming it.

FIX (Day 9): the Oracle connection used to be created at the TOP LEVEL of
this file (outside any function). That meant simply IMPORTING this file -
which api.py does - tried to connect to Oracle immediately, even before
anyone called /migrate/start. If Oracle wasn't running yet, the whole API
crashed on startup instead of just failing the one migration attempt that
actually needed it. Fixed by moving the connection inside read_schema()
so it only connects when the function is actually called.
"""

import oracledb

from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN
from constraints import is_not_null_check

# We only care about the tables *we* created, not Oracle's own SYSTEM
# tables (there are hundreds of those). List them explicitly for now;
# Day 3+ can switch this to "every table owned by a given app schema"
# once we're not sharing the SYSTEM user for our own tables.
OUR_TABLES = ["MEMBERS", "BOOKS", "LOANS"]


def get_oracle_connection():
    """
    Creates a fresh Oracle connection on demand, rather than at import
    time. Callers (read_schema(), or the __main__ block below) each get
    their own connection and are responsible for closing it.
    """
    return oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN,
    )


def get_columns(cursor, table_name):
    cursor.execute(
        """
        SELECT column_name, data_type, data_length, data_precision, data_scale, nullable, data_default
        FROM all_tab_columns
        WHERE table_name = :table_name
          AND owner = 'SYSTEM'
        ORDER BY column_id
        """,
        table_name=table_name,
    )
    columns = []
    for col_name, data_type, data_length, precision, scale, nullable, default in cursor.fetchall():
        columns.append({
            "name": col_name,
            "type": data_type,
            "length": data_length,
            "precision": precision,   # NULL for bare NUMBER and non-numeric types
            "scale": scale,
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


def get_unique_constraints(cursor, table_name):
    """UNIQUE constraints (constraint_type 'U'), grouped so a multi-column
    UNIQUE comes back as ONE constraint with several columns."""
    cursor.execute(
        """
        SELECT ac.constraint_name, acc.column_name
        FROM all_constraints ac
        JOIN all_cons_columns acc
          ON ac.constraint_name = acc.constraint_name
         AND ac.owner = acc.owner
        WHERE ac.table_name = :table_name
          AND ac.owner = 'SYSTEM'
          AND ac.constraint_type = 'U'
        ORDER BY ac.constraint_name, acc.position
        """,
        table_name=table_name,
    )
    grouped = {}
    for constraint_name, column_name in cursor.fetchall():
        grouped.setdefault(constraint_name, []).append(column_name)
    return [{"name": name, "columns": cols} for name, cols in grouped.items()]


def get_check_constraints(cursor, table_name):
    """CHECK constraints (constraint_type 'C'). Oracle also stores every
    NOT NULL as a 'C' constraint; those are filtered out because nullability
    is already migrated separately."""
    cursor.execute(
        """
        SELECT constraint_name, search_condition
        FROM all_constraints
        WHERE table_name = :table_name
          AND owner = 'SYSTEM'
          AND constraint_type = 'C'
        ORDER BY constraint_name
        """,
        table_name=table_name,
    )
    checks = []
    for constraint_name, condition in cursor.fetchall():
        if condition is None or is_not_null_check(condition):
            continue
        checks.append({"name": constraint_name, "condition": condition.strip()})
    return checks


def read_schema():
    """
    Opens its own Oracle connection, reads all OUR_TABLES' metadata,
    then closes the connection before returning - so callers (like
    api.py) don't need to manage the connection lifecycle themselves.
    """
    connection = get_oracle_connection()
    cursor = connection.cursor()
    schema = {}

    try:
        for table_name in OUR_TABLES:
            schema[table_name] = {
                "columns": get_columns(cursor, table_name),
                "primary_key": get_primary_key(cursor, table_name),
                "foreign_keys": get_foreign_keys(cursor, table_name),
                "unique_constraints": get_unique_constraints(cursor, table_name),
                "check_constraints": get_check_constraints(cursor, table_name),
            }
    finally:
        cursor.close()
        connection.close()

    return schema


def print_schema(schema):
    for table_name, details in schema.items():
        print(f"\n=== {table_name} ===")
        print(f"Primary key: {details['primary_key']}")

        print("Columns:")
        for col in details["columns"]:
            nullable = "NULL" if col["nullable"] else "NOT NULL"
            default = f" DEFAULT {col['default']}" if col["default"] else ""
            declared = ""
            if col.get("precision") is not None or col.get("scale") is not None:
                declared = f" [precision={col.get('precision')}, scale={col.get('scale')}]"
            print(f"  - {col['name']}: {col['type']}({col['length']}){declared} {nullable}{default}")

        if details["foreign_keys"]:
            print("Foreign keys:")
            for fk in details["foreign_keys"]:
                print(f"  - {fk['column']} -> {fk['references_table']}.{fk['references_column']}")

        if details.get("unique_constraints"):
            print("Unique constraints:")
            for uc in details["unique_constraints"]:
                print(f"  - {uc['name']}: {', '.join(uc['columns'])}")

        if details.get("check_constraints"):
            print("Check constraints:")
            for cc in details["check_constraints"]:
                print(f"  - {cc['name']}: {cc['condition']}")


if __name__ == "__main__":
    schema = read_schema()
    print_schema(schema)