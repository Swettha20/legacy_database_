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

from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN, ORACLE_SCHEMA, ORACLE_TABLES
from constraints import is_not_null_check, fk_columns

# Safety valve for automatic table discovery. A normal application schema has
# a handful to a few hundred tables; a schema like SYSTEM that also holds
# Oracle's own tables must not be migrated wholesale by accident.
MAX_AUTO_DISCOVERED_TABLES = 200


class SchemaReadError(RuntimeError):
    """The Oracle schema could not be read as configured."""


def discover_tables(cursor):
    """
    Every table owned by ORACLE_SCHEMA that was created by a user, not by
    Oracle's own install scripts (oracle_maintained = 'N'), minus
    recycle-bin and other system-internal leftovers.
    """
    try:
        cursor.execute(
            """
            SELECT object_name
            FROM all_objects
            WHERE owner = :owner
              AND object_type = 'TABLE'
              AND oracle_maintained = 'N'
              AND object_name NOT LIKE 'BIN$%'
              AND object_name NOT LIKE 'DR$%'
              AND object_name NOT LIKE 'MLOG$%'
              AND object_name NOT LIKE 'RUPD$%'
              AND object_name NOT LIKE 'SYS_IOT_OVER_%'
              AND object_name NOT LIKE 'SYS_EXPORT_%'
            ORDER BY object_name
            """,
            owner=ORACLE_SCHEMA,
        )
    except oracledb.DatabaseError as e:
        raise SchemaReadError(
            f"Could not discover tables in schema {ORACLE_SCHEMA} ({e}). "
            f"Set ORACLE_TABLES in .env to list them explicitly, e.g. "
            f"ORACLE_TABLES=MEMBERS,BOOKS,LOANS"
        ) from e
    return [row[0] for row in cursor.fetchall()]


def get_table_names(cursor):
    """The explicit ORACLE_TABLES list if set, otherwise discovery."""
    if ORACLE_TABLES:
        return list(ORACLE_TABLES)
    tables = discover_tables(cursor)
    if not tables:
        raise SchemaReadError(f"No user tables found in schema {ORACLE_SCHEMA}.")
    if len(tables) > MAX_AUTO_DISCOVERED_TABLES:
        raise SchemaReadError(
            f"Schema {ORACLE_SCHEMA} has {len(tables)} tables - too many to migrate "
            f"by accident. Set ORACLE_TABLES in .env to choose which ones."
        )
    return tables


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
        SELECT column_name, data_type, data_length, char_length, data_precision, data_scale, nullable, data_default
        FROM all_tab_columns
        WHERE table_name = :table_name
          AND owner = :owner
        ORDER BY column_id
        """,
        table_name=table_name,
        owner=ORACLE_SCHEMA,
    )
    columns = []
    for col_name, data_type, data_length, char_length, precision, scale, nullable, default in cursor.fetchall():
        columns.append({
            "name": col_name,
            "type": data_type,
            "length": data_length,
            "char_length": char_length or None,   # declared length in CHARACTERS (data_length is bytes)
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
          AND ac.owner = :owner
          AND ac.constraint_type = 'P'
        ORDER BY acc.position
        """,
        table_name=table_name,
        owner=ORACLE_SCHEMA,
    )
    return [row[0] for row in cursor.fetchall()]


def get_foreign_keys(cursor, table_name):
    """
    One entry per foreign key CONSTRAINT, with all its columns in order. (A
    key spanning two columns used to come back as two unrelated one-column
    keys, which is a different - and wrong - constraint.) Also keeps the
    ON DELETE rule, which used to be dropped silently.
    """
    cursor.execute(
        """
        SELECT ac.constraint_name, acc.column_name, r_ac.table_name AS ref_table,
               r_acc.column_name AS ref_column, ac.delete_rule
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
          AND ac.owner = :owner
          AND ac.constraint_type = 'R'
        ORDER BY ac.constraint_name, acc.position
        """,
        table_name=table_name,
        owner=ORACLE_SCHEMA,
    )
    grouped = {}
    for name, column, ref_table, ref_column, delete_rule in cursor.fetchall():
        fk = grouped.setdefault(name, {
            "name": name, "columns": [], "references_table": ref_table,
            "references_columns": [], "on_delete": delete_rule,
        })
        fk["columns"].append(column)
        fk["references_columns"].append(ref_column)
    return list(grouped.values())


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
          AND ac.owner = :owner
          AND ac.constraint_type = 'U'
        ORDER BY ac.constraint_name, acc.position
        """,
        table_name=table_name,
        owner=ORACLE_SCHEMA,
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
          AND owner = :owner
          AND constraint_type = 'C'
        ORDER BY constraint_name
        """,
        table_name=table_name,
        owner=ORACLE_SCHEMA,
    )
    checks = []
    for constraint_name, condition in cursor.fetchall():
        if condition is None or is_not_null_check(condition):
            continue
        checks.append({"name": constraint_name, "condition": condition.strip()})
    return checks


def get_unmigrated_objects(cursor):
    """
    Things in the schema that this tool does NOT migrate. They are listed in
    the report so they are never silently lost: ordinary indexes, views,
    triggers, standalone sequences, and stored code.
    """
    found = {}

    def fetch(label, sql, **binds):
        cursor.execute(sql, owner=ORACLE_SCHEMA, **binds)
        rows = [row[0] if len(row) == 1 else " / ".join(str(x) for x in row) for row in cursor.fetchall()]
        if rows:
            found[label] = rows

    fetch("indexes (non-unique, not backing a constraint)", """
        SELECT index_name || ' on ' || table_name FROM all_indexes
        WHERE owner = :owner AND uniqueness = 'NONUNIQUE'
          AND index_name NOT IN (SELECT index_name FROM all_constraints
                                 WHERE owner = :owner AND index_name IS NOT NULL)
          AND index_name NOT LIKE 'SYS_IL%' AND index_name NOT LIKE 'SYS_IOT%'
          AND table_name IN (SELECT object_name FROM all_objects
                             WHERE owner = :owner AND object_type = 'TABLE' AND oracle_maintained = 'N')
        ORDER BY 1""")
    for label, object_type in [("views", "VIEW"), ("triggers", "TRIGGER"),
                               ("stored procedures", "PROCEDURE"), ("stored functions", "FUNCTION"),
                               ("packages", "PACKAGE")]:
        fetch(label, """
            SELECT object_name FROM all_objects
            WHERE owner = :owner AND object_type = :object_type AND oracle_maintained = 'N'
            ORDER BY 1""", object_type=object_type)
    fetch("standalone sequences", """
        SELECT object_name FROM all_objects
        WHERE owner = :owner AND object_type = 'SEQUENCE' AND oracle_maintained = 'N'
          AND object_name NOT LIKE 'ISEQ$$%'
        ORDER BY 1""")
    return found


def read_source_objects():
    """
    Source code of every user-created stored procedure and function in the
    schema, ready to translate. ALL_SOURCE stores the code line by line, and
    starts at the keyword PROCEDURE / FUNCTION (no CREATE OR REPLACE).
    """
    connection = get_oracle_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT s.name, s.type, s.text
            FROM all_source s
            WHERE s.owner = :owner
              AND s.type IN ('PROCEDURE', 'FUNCTION')
              AND s.name IN (SELECT object_name FROM all_objects
                             WHERE owner = :owner
                               AND object_type IN ('PROCEDURE', 'FUNCTION')
                               AND oracle_maintained = 'N')
            ORDER BY s.name, s.type, s.line
            """,
            owner=ORACLE_SCHEMA,
        )
        lines = {}
        for name, object_type, text in cursor.fetchall():
            lines.setdefault((name, object_type), []).append(text)
        return [{"name": name, "type": object_type,
                 "source": "CREATE OR REPLACE " + "".join(parts).strip()}
                for (name, object_type), parts in lines.items()]
    finally:
        cursor.close()
        connection.close()


def read_unmigrated_objects():
    """
    Informational only: if this inventory fails for any reason the migration
    must still go ahead, so a failure is returned as a note, never raised.
    """
    connection = get_oracle_connection()
    cursor = connection.cursor()
    try:
        return get_unmigrated_objects(cursor)
    except Exception as e:
        return {"inventory could not be read": [str(e).splitlines()[0]]}
    finally:
        cursor.close()
        connection.close()


def read_schema():
    """
    Opens its own Oracle connection, reads the metadata of every table to migrate,
    then closes the connection before returning - so callers (like
    api.py) don't need to manage the connection lifecycle themselves.
    """
    connection = get_oracle_connection()
    cursor = connection.cursor()
    schema = {}

    try:
        for table_name in get_table_names(cursor):
            columns = get_columns(cursor, table_name)
            if not columns:
                raise SchemaReadError(
                    f"Table {table_name} was not found in schema {ORACLE_SCHEMA} "
                    f"(check ORACLE_SCHEMA / ORACLE_TABLES in .env)."
                )
            schema[table_name] = {
                "columns": columns,
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
                cols, refs = fk_columns(fk)
                rule = f" ON DELETE {fk['on_delete']}" if fk.get("on_delete") not in (None, "NO ACTION") else ""
                print(f"  - ({', '.join(cols)}) -> {fk['references_table']}({', '.join(refs)}){rule}")

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
    unmigrated = read_unmigrated_objects()
    if unmigrated:
        print("\n=== NOT migrated by this tool (listed so nothing is lost silently) ===")
        for kind, names in unmigrated.items():
            print(f"{kind}:")
            for n in names:
                print(f"  - {n}")