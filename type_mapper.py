"""
legacy-db-modernizer: Day 4-5, updated Day 9 (FK fix) and Day 11 (AI advisor)
Rule-based Oracle -> PostgreSQL type mapping, with an AI-assisted fallback
for genuinely ambiguous cases.

History of fixes, kept here since each one taught something real:
- Day 6 surfaced a bug: foreign keys got the generic ambiguous NUMBER
  treatment even when they referenced a SERIAL primary key, causing a
  Postgres DatatypeMismatch error. Fixed with a two-pass approach: map
  primary keys first, then match foreign keys to their referenced type.
- Day 11: for remaining ambiguous bare NUMBER columns (not foreign keys,
  no inferable type), instead of defaulting straight to NUMERIC, ask the
  local AI model for a more informed suggestion based on the column name,
  validated against a fixed allowlist before being trusted. Confidence
  stays "needs_review" either way - an AI guess is still a guess.
"""

import json
from ai_type_advisor import suggest_type_for_ambiguous_column
from constraints import translate_default, translate_check


def map_column_type(table_name: str, column: dict) -> dict:
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
        # Ambiguous case - ask the AI advisor for a more informed guess
        # than a blind NUMERIC default. Still flagged needs_review either way.
        print(f"  Asking AI advisor for {table_name}.{column['name']}...")
        return suggest_type_for_ambiguous_column(table_name, column["name"])

    return {
        "pg_type": None,
        "confidence": "manual_action_needed",
        "note": f"No mapping rule exists yet for Oracle type '{source_type}'",
    }


def map_schema(ir: dict) -> dict:
    mapped = {"tables": []}

    # --- Pass 1: map every table's columns with the base rules (including
    # AI advisor calls for ambiguous cases), and remember what type each
    # table's PRIMARY KEY column resolved to.
    primary_key_types = {}

    table_mappings = {}
    for table in ir["tables"]:
        column_map = {}
        for col in table["columns"]:
            column_map[col["name"]] = map_column_type(table["name"], col)
        table_mappings[table["name"]] = column_map

        primary_key_types[table["name"]] = {
            pk: column_map[pk]["pg_type"] for pk in table["primary_key"]
        }

    # --- Pass 2: for every foreign key, override with the exact type of
    # the column it references - this takes priority over anything Pass 1
    # decided (including an AI suggestion), since it's derivable with
    # certainty from data already in the IR, not a guess.
    for table in ir["tables"]:
        for fk in table["foreign_keys"]:
            ref_table = fk["references_table"]
            ref_col = fk["references_column"]
            ref_type = primary_key_types.get(ref_table, {}).get(ref_col)

            if ref_type:
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
        column_names = [c["name"] for c in table["columns"]]
        mapped_table = {
            "name": table["name"],
            "primary_key": table["primary_key"],
            "foreign_keys": table["foreign_keys"],
            "unique_constraints": [
                uc for uc in table.get("unique_constraints", [])
                if all(c in column_names for c in uc["columns"])
            ],
            "check_constraints": [],
            "columns": [],
        }
        for check in table.get("check_constraints", []):
            pg_condition, note = translate_check(check["condition"], column_names)
            mapped_table["check_constraints"].append({
                "name": check["name"],
                "oracle_condition": check["condition"],
                "pg_condition": pg_condition,
                "note": note,
            })
        for col in table["columns"]:
            mapping = table_mappings[table["name"]][col["name"]]
            mapped_col = {
                "name": col["name"],
                "source_type": col["type"],
                "nullable": col["nullable"],
                **mapping,
            }
            # Auto-increment columns get their default from SERIAL itself.
            if col.get("default") and not col.get("is_auto_increment"):
                pg_default, default_note = translate_default(col["default"])
                mapped_col["oracle_default"] = col["default"].strip()
                mapped_col["pg_default"] = pg_default
                mapped_col["default_note"] = default_note
            mapped_table["columns"].append(mapped_col)
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
            if col.get("oracle_default"):
                if col.get("pg_default"):
                    print(f"          default: {col['oracle_default']} -> {col['pg_default']}")
                else:
                    print(f"          [REVIEW] default not carried over: {col['default_note']}")
        for uc in table.get("unique_constraints", []):
            print(f"  [OK] UNIQUE ({', '.join(uc['columns'])})")
        for cc in table.get("check_constraints", []):
            if cc["pg_condition"]:
                print(f"  [OK] CHECK ({cc['pg_condition']})")
            else:
                print(f"  [REVIEW] CHECK ({cc['oracle_condition']}) not carried over: {cc['note']}")


if __name__ == "__main__":
    with open("schema_ir.json") as f:
        ir = json.load(f)

    mapped = map_schema(ir)
    print_confidence_report(mapped)

    with open("mapped_schema.json", "w") as f:
        json.dump(mapped, f, indent=2)
    print("\nSaved to mapped_schema.json")