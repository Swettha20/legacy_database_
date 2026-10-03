"""
legacy-db-modernizer: loads sample_procedures.sql into your Oracle schema so
the translator has varied real code to work on, then reports any Oracle
compile errors.

    python load_sample_procedures.py          # create them
    python load_sample_procedures.py --drop   # remove them again
"""

import sys

import oracledb

from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN, ORACLE_SCHEMA
from plsql_translator import split_plsql_script


def main(argv):
    objects = split_plsql_script(open("sample_procedures.sql", encoding="utf-8").read())
    connection = oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)
    cursor = connection.cursor()
    try:
        if "--drop" in argv:
            for obj in objects:
                try:
                    cursor.execute(f"DROP {obj['type']} {obj['name']}")
                    print(f"  dropped {obj['type']} {obj['name']}")
                except oracledb.DatabaseError:
                    print(f"  {obj['name']} did not exist")
            return 0

        for obj in objects:
            cursor.execute(obj["source"])
            print(f"  created {obj['type']} {obj['name']}")

        # Oracle reports "created with compilation errors" as a warning, not an exception
        cursor.execute(
            "SELECT name, line, text FROM all_errors WHERE owner = :owner AND name IN ("
            + ", ".join(f"'{o['name']}'" for o in objects) + ") ORDER BY name, sequence",
            owner=ORACLE_SCHEMA,
        )
        errors = cursor.fetchall()
        if errors:
            print("\nOracle reported compile errors:")
            for name, line, text in errors:
                print(f"  {name} line {line}: {text.strip()}")
            return 1
        print(f"\nAll {len(objects)} compiled cleanly. Now run:  python plsql_translator.py")
        return 0
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
