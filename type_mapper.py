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
from constraints import translate_default, translate_check, fk_columns
import re


def map_declared_number(precision, scale):
    """
    Deterministic mapping for a NUMBER that DECLARES its precision/scale.
    Returns a mapping dict, or None for a bare NUMBER (no precision, no
    scale) - the only case that genuinely needs the AI advisor.

    Before this, every NUMBER was treated as bare, so NUMBER(10,2) was sent
    to the AI as if nothing were known about it, and could come back as
    INTEGER (silently rounding prices).
    """
    if precision is None and scale is None:
        return None                                   # bare NUMBER

    if scale is None:
        scale = 0     # NUMBER(p) means scale 0

    declared = f"NUMBER({precision if precision is not None else '*'},{scale})"

    if scale is not None and scale < 0:
        return {
            "pg_type": "NUMERIC",
            "confidence": "needs_review",
            "note": f"Declared {declared} rounds to tens/hundreds, which has no direct "
                    f"Postgres equivalent - mapped to unconstrained NUMERIC, please confirm",
        }

    if scale == 0:
        if precision is None:                         # NUMBER(*,0): any whole number up to 38 digits
            return {"pg_type": "NUMERIC(38)", "confidence": "high",
                    "note": f"Declared {declared}: whole numbers of up to 38 digits - NUMERIC(38) holds them all"}
        if precision <= 9:
            pg, why = "INTEGER", "fits a 32-bit INTEGER (up to 9 digits)"
        elif precision <= 18:
            pg, why = "BIGINT", "fits a 64-bit BIGINT (up to 18 digits)"
        else:
            pg, why = f"NUMERIC({precision})", "too many digits for BIGINT, so exact NUMERIC"
        return {"pg_type": pg, "confidence": "high",
                "note": f"Declared NUMBER({precision}) is whole-number-only and {why}"}

    # scale > 0
    p = precision if precision is not None else 38
    return {"pg_type": f"NUMERIC({p},{scale})", "confidence": "high",
            "note": f"Declared {declared} maps to NUMERIC({p},{scale}) - same precision and scale"}


def map_other_type(source_type: str, column: dict):
    """
    Deterministic rules for the common non-NUMBER Oracle types. Returns a
    mapping dict, or None if the type has no rule (it is then reported as
    needing manual action - never guessed).

    Text lengths come from the declared length in CHARACTERS (char_length),
    not data_length (bytes): VARCHAR2(10 CHAR) holds 40 bytes in a UTF-8
    database and used to become VARCHAR(40).
    """
    text_length = column.get("char_length") or column["length"]
    t = source_type.upper()

    def ok(pg, note, confidence="high"):
        return {"pg_type": pg, "confidence": confidence, "note": note}

    if t == "VARCHAR2" or t == "VARCHAR" or t == "NVARCHAR2":
        return ok(f"VARCHAR({text_length})", "Direct equivalent - Postgres VARCHAR behaves the same way")
    if t == "CHAR" or t == "NCHAR":
        return ok(f"CHAR({text_length})", "Direct equivalent - fixed-length, blank-padded in both databases")
    if t in ("CLOB", "NCLOB", "LONG"):
        return ok("TEXT", f"Oracle {source_type} (large text) maps to Postgres TEXT, which has no size limit")
    if t in ("BLOB", "LONG RAW") or t == "RAW":
        return ok("BYTEA", f"Oracle {source_type} (binary) maps to Postgres BYTEA")
    if t == "BINARY_FLOAT":
        return ok("REAL", "32-bit IEEE float in both databases")
    if t == "BINARY_DOUBLE":
        return ok("DOUBLE PRECISION", "64-bit IEEE float in both databases")
    if t == "FLOAT":
        return ok("NUMERIC", "Oracle FLOAT is stored as an exact decimal number; unconstrained NUMERIC keeps "
                             "every digit. Use DOUBLE PRECISION instead if speed matters more than exactness",
                  "needs_review")

    m = re.fullmatch(r"TIMESTAMP\((\d)\)( WITH (LOCAL )?TIME ZONE)?", t)
    if m:
        digits, tz, local = int(m.group(1)), m.group(2), m.group(3)
        pg_digits = min(digits, 6)
        base = "TIMESTAMPTZ" if tz else "TIMESTAMP"
        pg = f"{base}({pg_digits})"
        problems = []
        if digits > 6:
            problems.append(f"Oracle keeps {digits} fractional-second digits, Postgres keeps at most 6")
        if tz:
            problems.append("time zone handling should be checked after loading"
                            + (" (LOCAL TIME ZONE depends on the session time zone)" if local else ""))
        if problems:
            return ok(pg, "; ".join(problems), "needs_review")
        return ok(pg, "Direct equivalent")
    return None


def map_column_type(table_name: str, column: dict) -> dict:
    source_type = column["type"]
    length = column["length"]
    is_auto_increment = column["is_auto_increment"]

    if is_auto_increment:
        # A 32-bit SERIAL overflows past ~2.1 billion; if the Oracle column
        # DECLARES more than 9 digits, a 64-bit BIGSERIAL is required.
        if (column.get("precision") or 0) > 9:
            return {
                "pg_type": "BIGSERIAL",
                "confidence": "high",
                "note": f"Auto-increment column declared NUMBER({column['precision']}) "
                        f"- needs 64-bit BIGSERIAL, not SERIAL",
            }
        return {
            "pg_type": "SERIAL",
            "confidence": "high",
            "note": "Auto-increment column (Oracle sequence.nextval) mapped to SERIAL",
        }

    if source_type == "DATE":
        return {
            "pg_type": "TIMESTAMP",
            "confidence": "high",
            "note": "Oracle DATE always includes a time component, so TIMESTAMP "
                    "is the correct equivalent (not Postgres DATE, which is date-only)",
        }

    if source_type == "NUMBER":
        declared = map_declared_number(column.get("precision"), column.get("scale"))
        if declared is not None:
            return declared
        # Ambiguous case (bare NUMBER, nothing declared) - ask the AI advisor for a more informed guess
        # than a blind NUMERIC default. Still flagged needs_review either way.
        print(f"  Asking AI advisor for {table_name}.{column['name']}...")
        return suggest_type_for_ambiguous_column(table_name, column["name"])

    other = map_other_type(source_type, column)
    if other is not None:
        return other

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

    # --- Pass 2: for every foreign key column, override with the exact type of
    # the column it references - this takes priority over anything Pass 1
    # decided (including an AI suggestion), since it's derivable with
    # certainty from data already in the IR, not a guess. Works for keys that
    # span several columns, and for keys that point at a UNIQUE column rather
    # than the primary key. Tables are processed parents-first, so a
    # referenced column has already received its own final type.
    for table in ir["tables"]:
        for fk in table["foreign_keys"]:
            ref_table = fk["references_table"]
            columns, ref_columns = fk_columns(fk)
            for column, ref_column in zip(columns, ref_columns):
                ref_type = table_mappings.get(ref_table, {}).get(ref_column, {}).get("pg_type")
                if not ref_type:
                    continue
                fk_pg_type = {"SERIAL": "INTEGER", "BIGSERIAL": "BIGINT"}.get(ref_type, ref_type)
                table_mappings[table["name"]][column] = {
                    "pg_type": fk_pg_type,
                    "confidence": "high",
                    "note": f"Foreign key referencing {ref_table}.{ref_column} "
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

    if ir.get("not_migrated"):
        mapped["not_migrated"] = ir["not_migrated"]
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