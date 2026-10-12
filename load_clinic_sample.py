"""
legacy-db-modernizer: creates the second sample database (a medical clinic)
in its own Oracle schema, CLINIC, so it never mixes with the library sample.

    python load_clinic_sample.py          # (re)create it: user, tables, data, procedures
    python load_clinic_sample.py --drop   # remove the CLINIC schema again

It connects as your normal Oracle user (SYSTEM) only to create the CLINIC user,
then does everything else as CLINIC. Set a password for that throwaway local
user in .env:   CLINIC_PASSWORD=...   (letters, digits, _ # $ only)
"""

import os
import re
import sys

import oracledb
from dotenv import load_dotenv

import sample_clinic as C
from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN
from plsql_translator import split_plsql_script

load_dotenv()


def _password():
    password = os.getenv("CLINIC_PASSWORD")
    if not password:
        raise SystemExit("CLINIC_PASSWORD is not set. Add a line like CLINIC_PASSWORD=Clinic_demo_1 to your .env "
                         "(it is only the password of a throwaway local sample user).")
    if not re.fullmatch(r"[A-Za-z0-9_#$]+", password):
        raise SystemExit("CLINIC_PASSWORD may only contain letters, digits and _ # $")
    return password


def drop_user(admin_cursor):
    admin_cursor.execute("SELECT COUNT(*) FROM all_users WHERE username = 'CLINIC'")
    if admin_cursor.fetchone()[0]:
        admin_cursor.execute("DROP USER clinic CASCADE")
        print("  dropped the CLINIC schema")
    else:
        print("  the CLINIC schema did not exist")


def main(argv) -> int:
    admin = oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)
    acur = admin.cursor()
    try:
        if "--drop" in argv:
            drop_user(acur)
            return 0
        password = _password()
        drop_user(acur)                     # always start clean, so re-running is safe
        acur.execute(f'CREATE USER clinic IDENTIFIED BY "{password}" QUOTA UNLIMITED ON USERS')
        acur.execute("GRANT CREATE SESSION, CREATE TABLE, CREATE PROCEDURE, CREATE SEQUENCE TO clinic")
        print("  created the CLINIC user")
    finally:
        acur.close()
        admin.close()

    conn = oracledb.connect(user="clinic", password=password, dsn=ORACLE_DSN)
    cur = conn.cursor()
    try:
        for ddl in C.TABLES_DDL:
            cur.execute(ddl)
        print(f"  created {len(C.TABLES_DDL)} tables")
        for table, insert, rows in C.DATA:
            cur.executemany(insert, rows)
            print(f"  loaded {len(rows):>2} rows into {table}")
        conn.commit()
        objects = split_plsql_script(C.PROCEDURES_SQL)
        for obj in objects:
            cur.execute(obj["source"])
        print(f"  created {len(objects)} procedures/functions: {', '.join(o['name'].lower() for o in objects)}")
        cur.execute("SELECT name, line, text FROM user_errors ORDER BY name, sequence")
        errors = cur.fetchall()
        if errors:
            print("\nOracle reported compile errors:")
            for name, line, text in errors:
                print(f"  {name} line {line}: {text.strip()}")
            return 1
        print("\nThe clinic sample is ready (schema CLINIC). Run:  python run_clinic_demo.py")
        return 0
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
