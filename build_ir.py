"""
legacy-db-modernizer: Day 3
Converts the raw schema metadata from read_schema.py into a clean,
well-defined Intermediate Representation (IR) - a single JSON structure
describing tables, columns, types, and relationships.

This does NOT do Oracle -> PostgreSQL type mapping yet (that's Day 4-5).
It only reshapes and normalizes the structure so later phases have one
consistent, predictable format to work from, regardless of which source
database the metadata originally came from.

Design choice: table/column names are lowercased here. Oracle stores
identifiers in uppercase by default; PostgreSQL convention is lowercase
unless quoted. Normalizing once, here, means every later phase can just
trust the names are already clean instead of re-normalizing repeatedly.
"""

import json
from read_schema import read_schema


def build_table_ir(table_name, details):
    return {
        "name": table_name.lower(),
        "columns": [
            {
                "name": col["name"].lower(),
                "type": col["type"],          # raw source type - Day 4-5 maps this
                "length": col["length"],
                "precision": col.get("precision"),   # declared NUMBER(p,s); None = bare NUMBER
                "scale": col.get("scale"),
                "nullable": col["nullable"],
                "default": col["default"],    # raw default expression, kept as-is
                "is_auto_increment": bool(
                    col["default"] and ".nextval" in col["default"].lower()
                ),
            }
            for col in details["columns"]
        ],
        "primary_key": [pk.lower() for pk in details["primary_key"]],
        "foreign_keys": [
            {
                "column": fk["column"].lower(),
                "references_table": fk["references_table"].lower(),
                "references_column": fk["references_column"].lower(),
            }
            for fk in details["foreign_keys"]
        ],
        # .get(): older raw schemas (before these were read) simply have none
        "unique_constraints": [
            {"name": uc["name"].lower(), "columns": [c.lower() for c in uc["columns"]]}
            for uc in details.get("unique_constraints", [])
        ],
        "check_constraints": [
            {"name": cc["name"].lower(), "condition": cc["condition"]}
            for cc in details.get("check_constraints", [])
        ],
    }


class SchemaError(ValueError):
    """The schema cannot be migrated as it stands (reported before anything is written)."""


def order_tables(tables: list) -> list:
    """
    Orders tables so every table comes AFTER the tables its foreign keys
    point at (parents before children). Needed twice: CREATE TABLE fails if
    the referenced table does not exist yet, and rows cannot be loaded
    before the rows they reference. Discovery returns tables alphabetically,
    which is almost never a valid order.

    Ties keep the discovery order. A self-reference (employees.manager_id ->
    employees.id) is allowed and ignored for ordering. A true cycle between
    two or more tables is refused with a clear message instead of failing
    halfway through a migration.
    """
    by_name = {t["name"]: t for t in tables}
    deps = {}
    for t in tables:
        wanted = {fk["references_table"] for fk in t["foreign_keys"]}
        outside = sorted(w for w in wanted if w not in by_name)
        if outside:
            raise SchemaError(
                f"Table '{t['name']}' has a foreign key to '{outside[0]}', which is not part of "
                f"the migrated tables. Add it to ORACLE_TABLES (or migrate its whole schema)."
            )
        deps[t["name"]] = wanted - {t["name"]}

    ordered, placed = [], set()
    remaining = [t["name"] for t in tables]
    while remaining:
        ready = next((n for n in remaining if deps[n] <= placed), None)
        if ready is None:
            raise SchemaError(
                "Circular foreign keys between: " + ", ".join(remaining)
                + ". Circular references are not supported yet - break the cycle first."
            )
        ordered.append(by_name[ready])
        placed.add(ready)
        remaining.remove(ready)
    return ordered


def build_ir(schema: dict) -> dict:
    """
    Takes the raw {table_name: {...}} dict from read_schema() and returns
    a clean IR: {"tables": [ {...}, {...} ]} - a list, not a dict, so table
    order is preserved and later phases can iterate predictably.
    """
    return {
        "tables": order_tables([
            build_table_ir(table_name, details)
            for table_name, details in schema.items()
        ])
    }


def save_ir(ir: dict, path: str = "schema_ir.json"):
    with open(path, "w") as f:
        json.dump(ir, f, indent=2)
    print(f"IR written to {path}")


if __name__ == "__main__":
    schema = read_schema()
    ir = build_ir(schema)

    print(json.dumps(ir, indent=2))
    save_ir(ir)