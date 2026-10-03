"""
legacy-db-modernizer: Day 13
Generates a standalone, human-readable migration report from
mapped_schema.json - the actual "confidence/flagging report" the
project's architecture promised from day one. Groups every migrated
column by confidence level, so a reviewer can see at a glance what's
safe to trust and what needs a second look, without digging through
the raw JSON.
"""

import json
from datetime import datetime


def generate_report(mapped_schema: dict) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append("MIGRATION CONFIDENCE REPORT")
    lines.append(f"Generated: {datetime.now().isoformat()}")
    lines.append("=" * 60)

    counts = {"high": 0, "needs_review": 0, "manual_action_needed": 0}

    for table in mapped_schema["tables"]:
        for col in table["columns"]:
            counts[col["confidence"]] += 1

    total = sum(counts.values())
    lines.append(f"\nTotal columns migrated: {total}")
    lines.append(f"  High confidence (auto-migrated, safe):    {counts['high']}")
    lines.append(f"  Needs review (migrated, please confirm):  {counts['needs_review']}")
    lines.append(f"  Manual action needed (not auto-migrated): {counts['manual_action_needed']}")

    if counts["needs_review"] > 0:
        lines.append("\n" + "-" * 60)
        lines.append("ITEMS NEEDING REVIEW:")
        lines.append("-" * 60)
        for table in mapped_schema["tables"]:
            for col in table["columns"]:
                if col["confidence"] == "needs_review":
                    lines.append(f"\n  {table['name']}.{col['name']}")
                    lines.append(f"    {col['source_type']} -> {col['pg_type']}")
                    lines.append(f"    Reason: {col['note']}")

    if counts["manual_action_needed"] > 0:
        lines.append("\n" + "-" * 60)
        lines.append("ITEMS REQUIRING MANUAL ACTION:")
        lines.append("-" * 60)
        for table in mapped_schema["tables"]:
            for col in table["columns"]:
                if col["confidence"] == "manual_action_needed":
                    lines.append(f"\n  {table['name']}.{col['name']}")
                    lines.append(f"    Reason: {col['note']}")

    # ---- constraints and defaults (previously dropped silently) ----
    n_unique = n_check = n_default = 0
    review = []
    for table in mapped_schema["tables"]:
        n_unique += len(table.get("unique_constraints", []))
        for cc in table.get("check_constraints", []):
            if cc.get("pg_condition"):
                n_check += 1
            else:
                review.append(f"  {table['name']}: CHECK ({cc['oracle_condition']})\n"
                              f"    Not carried over: {cc['note']}")
        for col in table["columns"]:
            if col.get("oracle_default"):
                if col.get("pg_default"):
                    n_default += 1
                else:
                    review.append(f"  {table['name']}.{col['name']}: DEFAULT {col['oracle_default']}\n"
                                  f"    Not carried over: {col['default_note']}")

    lines.append("\n" + "-" * 60)
    lines.append("CONSTRAINTS AND DEFAULTS:")
    lines.append("-" * 60)
    lines.append(f"  UNIQUE constraints migrated: {n_unique}")
    lines.append(f"  CHECK constraints migrated:  {n_check}")
    lines.append(f"  Column defaults migrated:    {n_default}")
    has_constraint_data = any("unique_constraints" in t for t in mapped_schema["tables"])
    if not has_constraint_data:
        lines.append("  (This mapped_schema.json was produced before constraints were read,")
        lines.append("   so nothing is recorded here - re-run the migration to include them.)")
    elif review:
        lines.append(f"\n  NOT migrated - needs manual review ({len(review)}):")
        lines.extend("\n" + r for r in review)
    else:
        lines.append("  Nothing was left behind.")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    with open("mapped_schema.json") as f:
        mapped_schema = json.load(f)

    report = generate_report(mapped_schema)
    print(report)

    with open("migration_report.txt", "w") as f:
        f.write(report)
    print("\n\nSaved to migration_report.txt")