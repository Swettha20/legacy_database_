"""
legacy-db-modernizer: Day 4-5 (updated after Day 6 caught a real bug)
Rule-based Oracle -> PostgreSQL type mapping.

UPDATE: originally, any bare Oracle NUMBER column was mapped to NUMERIC
with a "needs_review" flag - including foreign key columns. Day 6 (writing
the schema into real Postgres) surfaced a real failure: Postgres refuses a
foreign key between a NUMERIC column and an INTEGER column (SERIAL primary
keys are INTEGER under the hood), even though the values would fit. Fixed
by first resolving the type of every primary key, then checking - for each
foreign key column - what type its referenced column actually resolved to,
and matching it exactly. This is now a "high" confidence decision, not a
guess, because it's inferable from data already in the IR.
"""

import json


def map_column_type(column: dict) -> dict:
    source_type = column["type"]
    length = column["length"]
    is_auto_increment = column["is_auto_increment"]

    if is_auto_increment:
        return {
            "pg_type": "SERIAL",
            "confidence": "high",
            "note": "Auto-increment column (Oracle sequence.nextval) mapped to SERIAL",
        }

    if source_type == "VARCHAR2":
        return {
            "pg_type": f"VARCHAR({length})",
            "confidence": "high",
            "note": "Direct equivalent - Postgres VARCHAR behaves the same way",
        }

    if source_type == "DATE":
        return {
            "pg_type": "TIMESTAMP",
            "confidence": "high",
            "note": "Oracle DATE always includes a time component, so TIMESTAMP "
                    "is the correct equivalent (not Postgres DATE, which is date-only)",
        }

    if source_type == "NUMBER":
        return {
            "pg_type": "NUMERIC",
            "confidence": "needs_review",
            "note": "Oracle NUMBER has no fixed precision/scale visible here - "
                    "defaulted to NUMERIC (safe for both integers and decimals), "
                    "but please confirm this is the intended type",
        }

    return {
        "pg_type": None,
        "confidence": "manual_action_needed",
        "note": f"No mapping rule exists yet for Oracle type '{source_type}'",
    }


def map_schema(ir: dict) -> dict:
    mapped = {"tables": []}

    # --- Pass 1: map every table's columns with the base rules, and
    # remember what type each table's PRIMARY KEY column resolved to.
    # We need this before Pass 2, since a foreign key's correct type
    # depends on knowing its target table's primary key type already.
    primary_key_types = {}  # table_name -> {column_name: pg_type}

    table_mappings = {}
    for table in ir["tables"]:
        column_map = {}
        for col in table["columns"]:
            column_map[col["name"]] = map_column_type(col)
        table_mappings[table["name"]] = column_map

        primary_key_types[table["name"]] = {
            pk: column_map[pk]["pg_type"] for pk in table["primary_key"]
        }

    # --- Pass 2: for every foreign key, override the generic mapping
    # with the exact type of the column it references - this is what
    # fixes the SERIAL/INTEGER vs NUMERIC mismatch Day 6 hit.
    for table in ir["tables"]:
        for fk in table["foreign_keys"]:
            ref_table = fk["references_table"]
            ref_col = fk["references_column"]
            ref_type = primary_key_types.get(ref_table, {}).get(ref_col)

            if ref_type:
                # SERIAL is INTEGER under the hood - a foreign key can't
                # be SERIAL itself (that would create its own sequence,
                # which is wrong for a column that should just hold a
                # copy of another table's id), so we use INTEGER here.
                fk_pg_type = "INTEGER" if ref_type == "SERIAL" else ref_type

                table_mappings[table["name"]][fk["column"]] = {
                    "pg_type": fk_pg_type,
                    "confidence": "high",
                    "note": f"Foreign key referencing {ref_table}.{ref_col} "
                            f"({ref_type}) - matched to {fk_pg_type} so the "
                            f"foreign key constraint is valid in Postgres",
                }

    # --- Assemble final output ---
    for table in ir["tables"]:
        mapped_table = {
            "name": table["name"],
            "primary_key": table["primary_key"],
            "foreign_keys": table["foreign_keys"],
            "columns": [],
        }
        for col in table["columns"]:
            mapping = table_mappings[table["name"]][col["name"]]
            mapped_table["columns"].append({
                "name": col["name"],
                "source_type": col["type"],
                "nullable": col["nullable"],
                **mapping,
            })
        mapped["tables"].append(mapped_table)

    return mapped


def print_confidence_report(mapped: dict):
    for table in mapped["tables"]:
        print(f"\n=== {table['name']} ===")
        for col in table["columns"]:
            icon = {
                "high": "OK",
                "needs_review": "REVIEW",
                "manual_action_needed": "MANUAL",
            }[col["confidence"]]
            print(f"  [{icon}] {col['name']}: {col['source_type']} -> {col['pg_type']}")
            print(f"          {col['note']}")


if __name__ == "__main__":
    with open("schema_ir.json") as f:
        ir = json.load(f)

    mapped = map_schema(ir)
    print_confidence_report(mapped)

    with open("mapped_schema.json", "w") as f:
        json.dump(mapped, f, indent=2)
    print("\nSaved to mapped_schema.json")