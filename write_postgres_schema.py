"""
legacy-db-modernizer: Day 6
Reads mapped_schema.json (the output of type_mapper.py) and generates +
executes real CREATE TABLE statements against PostgreSQL.

This is schema-only for now - no data yet (that's Day 7-8). The goal here
is to prove the full path end to end: Oracle schema -> IR -> type mapping
-> a real, running PostgreSQL schema.
"""

import json
import psycopg2

from config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME
from constraints import is_system_generated_name


def build_create_table_sql(table: dict) -> str:
    """
    Turns one table's mapped column info into a CREATE TABLE statement.
    Columns with pg_type == None (manual_action_needed) are skipped with
    a warning, rather than silently producing broken SQL.
    """
    column_lines = []

    for col in table["columns"]:
        if col["pg_type"] is None:
            print(f"  WARNING: skipping column '{col['name']}' - "
                  f"no type mapping available ({col['note']})")
            continue

        nullability = "" if col["nullable"] else " NOT NULL"
        default = f" DEFAULT {col['pg_default']}" if col.get("pg_default") else ""
        column_lines.append(f'    "{col["name"]}" {col["pg_type"]}{default}{nullability}')

    if table["primary_key"]:
        pk_cols = ", ".join(f'"{pk}"' for pk in table["primary_key"])
        column_lines.append(f"    PRIMARY KEY ({pk_cols})")

    usable_columns = {c["name"] for c in table["columns"] if c["pg_type"] is not None}

    for uc in table.get("unique_constraints", []):
        if not all(c in usable_columns for c in uc["columns"]):
            print(f"  WARNING: skipping UNIQUE on {uc['columns']} - a column has no type mapping")
            continue
        cols = ", ".join(f'"{c}"' for c in uc["columns"])
        name = "" if is_system_generated_name(uc["name"]) else f'CONSTRAINT "{uc["name"]}" '
        column_lines.append(f"    {name}UNIQUE ({cols})")

    for cc in table.get("check_constraints", []):
        if not cc.get("pg_condition"):
            continue  # untranslatable: reported by generate_report, never guessed
        name = "" if is_system_generated_name(cc["name"]) else f'CONSTRAINT "{cc["name"]}" '
        column_lines.append(f"    {name}CHECK ({cc['pg_condition']})")

    for fk in table["foreign_keys"]:
        column_lines.append(
            f'    FOREIGN KEY ("{fk["column"]}") '
            f'REFERENCES "{fk["references_table"]}"("{fk["references_column"]}")'
        )

    columns_sql = ",\n".join(column_lines)
    return f'CREATE TABLE "{table["name"]}" (\n{columns_sql}\n);'


def write_schema_to_postgres(mapped_schema: dict, connection):
    cursor = connection.cursor()

    # Drop tables first (in reverse order, so foreign keys don't block
    # the drop), so this script is safely re-runnable while developing.
    for table in reversed(mapped_schema["tables"]):
        cursor.execute(f'DROP TABLE IF EXISTS "{table["name"]}" CASCADE;')

    # Create in original order, since foreign keys need the referenced
    # table to already exist.
    for table in mapped_schema["tables"]:
        sql = build_create_table_sql(table)
        print(f"\n--- Creating table: {table['name']} ---")
        print(sql)
        cursor.execute(sql)

    connection.commit()
    cursor.close()
    print("\nAll tables created successfully in PostgreSQL.")


def get_pg_connection_for_api():
    """
    A reusable connection function, separate from the __main__ block below,
    so api.py (Day 9) can import and call this directly instead of
    duplicating connection code.
    """
    return psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        dbname=PG_DBNAME,
    )


if __name__ == "__main__":
    with open("mapped_schema.json") as f:
        mapped_schema = json.load(f)

    connection = get_pg_connection_for_api()

    write_schema_to_postgres(mapped_schema, connection)
    connection.close()