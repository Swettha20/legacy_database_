"""
legacy-db-modernizer: Day 3
Converts the Oracle-specific schema dictionary from read_schema.py into a
clean, source-agnostic IR (Intermediate Representation) - plain JSON that
later phases (type mapper, Postgres writer, AI translator) will read from,
instead of talking to Oracle directly.
"""

import json
from read_schema import read_schema


def build_ir(oracle_schema: dict) -> dict:
    """
    Takes the dict shape produced by read_schema.py (Oracle-specific:
    NUMBER/VARCHAR2 types, Oracle default syntax) and converts it into
    a clean, generic IR structure.
    """
    ir = {
        "source": "oracle",
        "tables": []
    }

    for table_name, details in oracle_schema.items():
        table_ir = {
            "name": table_name.lower(),
            "columns": [],
            "primary_key": [col.lower() for col in details["primary_key"]],
            "foreign_keys": [],
        }

        for col in details["columns"]:
            table_ir["columns"].append({
                "name": col["name"].lower(),
                "source_type": col["type"],       # kept as-is for now - Day 4-5 maps this
                "length": col["length"],
                "nullable": col["nullable"],
                "has_default": col["default"] is not None,
                "is_auto_increment": bool(
                    col["default"] and ".nextval" in col["default"].lower()
                ),
            })

        for fk in details["foreign_keys"]:
            table_ir["foreign_keys"].append({
                "column": fk["column"].lower(),
                "references_table": fk["references_table"].lower(),
                "references_column": fk["references_column"].lower(),
            })

        ir["tables"].append(table_ir)

    return ir


if __name__ == "__main__":
    oracle_schema = read_schema()
    ir = build_ir(oracle_schema)

    print(json.dumps(ir, indent=2))

    with open("schema_ir.json", "w") as f:
        json.dump(ir, f, indent=2)
    print("\nSaved to schema_ir.json")