"""
legacy-db-modernizer: Day 4-5
Rule-based Oracle -> PostgreSQL type mapping.

Deliberately NOT using AI here - type mapping for the common cases is a
solved, deterministic problem. A lookup table is faster, cheaper, and more
reliable than an AI call for these. AI is reserved for genuinely ambiguous
cases (flagged below with "needs_review" confidence) - same principle as
the detector being rule-based in the earlier ai-code-migrator project.

Every mapping decision gets a confidence level, feeding directly into the
project's confidence/flagging report:
    "high"         - unambiguous, safe to auto-apply
    "needs_review" - a reasonable default was chosen, but a human should
                      confirm (e.g. a bare NUMBER with no precision/scale)
"""

import json


def map_column_type(column: dict) -> dict:
    """
    Takes one column dict from the IR and returns a dict with the
    PostgreSQL type decision, confidence, and a human-readable note
    explaining the decision.
    """
    source_type = column["type"]
    length = column["length"]
    is_auto_increment = column["is_auto_increment"]

    # Auto-increment columns become SERIAL regardless of their Oracle type -
    # this takes priority over the normal type mapping below.
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
        # A bare NUMBER with no precision/scale info is genuinely ambiguous -
        # it could be an integer or hold decimals. We pick a safe default
        # (NUMERIC, which can hold both) but flag it for human review rather
        # than silently guessing.
        return {
            "pg_type": "NUMERIC",
            "confidence": "needs_review",
            "note": "Oracle NUMBER has no fixed precision/scale visible here - "
                    "defaulted to NUMERIC (safe for both integers and decimals), "
                    "but please confirm this is the intended type",
        }

    # Anything we don't have a rule for yet
    return {
        "pg_type": None,
        "confidence": "manual_action_needed",
        "note": f"No mapping rule exists yet for Oracle type '{source_type}'",
    }


def map_schema(ir: dict) -> dict:
    """
    Applies map_column_type() to every column in every table, returning
    a new structure with the mapping decisions attached - the original
    IR is left untouched.
    """
    mapped = {"tables": []}

    for table in ir["tables"]:
        mapped_table = {
            "name": table["name"],
            "primary_key": table["primary_key"],
            "foreign_keys": table["foreign_keys"],
            "columns": [],
        }

        for col in table["columns"]:
            mapping = map_column_type(col)
            mapped_table["columns"].append({
                "name": col["name"],
                "source_type": col["type"],
                "nullable": col["nullable"],
                **mapping,
            })

        mapped["tables"].append(mapped_table)

    return mapped


def print_confidence_report(mapped: dict):
    """
    A human-readable summary of every mapping decision, grouped by
    confidence level - this is the actual "confidence/flagging report"
    the project architecture promised.
    """
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