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
    }


def build_ir(schema: dict) -> dict:
    """
    Takes the raw {table_name: {...}} dict from read_schema() and returns
    a clean IR: {"tables": [ {...}, {...} ]} - a list, not a dict, so table
    order is preserved and later phases can iterate predictably.
    """
    return {
        "tables": [
            build_table_ir(table_name, details)
            for table_name, details in schema.items()
        ]
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