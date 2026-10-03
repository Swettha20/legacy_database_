"""
legacy-db-modernizer: volume test helper

Creates two scratch tables (VOL_CUSTOMERS and VOL_ORDERS, with a primary key,
a UNIQUE column, and a foreign key) in YOUR Oracle and fills them with
synthetic rows, so you can measure how fast a real migration runs on your
machine, including the Oracle read speed that a test elsewhere cannot see.

    python seed_oracle_volume.py 300000     # 300,000 customers + ~700,000 orders
    python seed_oracle_volume.py --drop     # remove the scratch tables again

The scratch tables are separate from the sample schema; pick them for a run
with the ORACLE_TABLES setting (see the README, "Measuring migration speed").
"""

import datetime
import decimal
import sys
import time

import oracledb

from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN

BATCH = 10_000
ORDERS_PER_CUSTOMER = 2.33


def connect():
    return oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)


def drop_tables(cursor):
    for table in ("VOL_ORDERS", "VOL_CUSTOMERS"):          # child first
        try:
            cursor.execute(f"DROP TABLE {table} PURGE")
            print(f"  dropped {table}")
        except oracledb.DatabaseError:
            print(f"  {table} did not exist")


def create_tables(cursor):
    cursor.execute("""
        CREATE TABLE VOL_CUSTOMERS (
            ID       NUMBER(9)      PRIMARY KEY,
            NAME     VARCHAR2(100)  NOT NULL,
            EMAIL    VARCHAR2(150)  NOT NULL UNIQUE,
            CREATED  DATE           NOT NULL,
            BALANCE  NUMBER(12,2)   NOT NULL,
            NOTES    VARCHAR2(400)
        )""")
    cursor.execute("""
        CREATE TABLE VOL_ORDERS (
            ID           NUMBER(9)    PRIMARY KEY,
            CUSTOMER_ID  NUMBER(9)    NOT NULL REFERENCES VOL_CUSTOMERS(ID),
            AMOUNT       NUMBER(10,2) NOT NULL,
            PLACED       TIMESTAMP(6) NOT NULL
        )""")


def customer_rows(n):
    base = datetime.datetime(2020, 1, 1)
    for i in range(1, n + 1):
        yield (i, f"Customer {i}", f"c{i}@example.com", base + datetime.timedelta(minutes=i),
               decimal.Decimal(i % 100000) / 100,
               None if i % 7 == 0 else 'some free-form note, with "quotes" and commas')


def order_rows(n_orders, n_customers):
    base = datetime.datetime(2020, 1, 1)
    for i in range(1, n_orders + 1):
        yield (i, (i % n_customers) + 1, decimal.Decimal(i % 99999) / 100,
               base + datetime.timedelta(seconds=i, microseconds=i % 1000000))


def insert_in_batches(cursor, sql, rows, label, total):
    batch, done, started = [], 0, time.time()
    for row in rows:
        batch.append(row)
        if len(batch) == BATCH:
            cursor.executemany(sql, batch)
            done += len(batch)
            batch = []
            if done % 100_000 == 0:
                print(f"  {label}: {done:,} / {total:,}")
    if batch:
        cursor.executemany(sql, batch)
        done += len(batch)
    print(f"  {label}: {done:,} rows in {time.time() - started:.1f}s")


def main(argv):
    connection = connect()
    cursor = connection.cursor()
    try:
        if "--drop" in argv:
            drop_tables(cursor)
            return
        n_customers = int(argv[1]) if len(argv) > 1 else 100_000
        n_orders = int(n_customers * ORDERS_PER_CUSTOMER)
        print(f"Creating scratch tables and {n_customers:,} customers + {n_orders:,} orders...")
        drop_tables(cursor)
        create_tables(cursor)
        insert_in_batches(cursor, "INSERT INTO VOL_CUSTOMERS VALUES (:1, :2, :3, :4, :5, :6)",
                          customer_rows(n_customers), "customers", n_customers)
        insert_in_batches(cursor, "INSERT INTO VOL_ORDERS VALUES (:1, :2, :3, :4)",
                          order_rows(n_orders, n_customers), "orders", n_orders)
        connection.commit()
        print("Done. Run the migration with ORACLE_TABLES=VOL_CUSTOMERS,VOL_ORDERS (see the README).")
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    main(sys.argv)
