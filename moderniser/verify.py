"""Bounded-memory, order-independent exact value verification, including duplicates."""
import datetime as dt
import decimal
import hashlib
import json
import math
import sqlite3
import tempfile
import re
import uuid
from pathlib import Path


def canonical(value):
    if value is None:
        return b'N'
    if hasattr(value, 'read'):
        value = value.read()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return b'B' + bytes(value)
    if isinstance(value, (int, decimal.Decimal)):
        number = decimal.Decimal(value)
        # normalize() uses the active decimal precision and can round 38-digit values.
        text = format(number, 'f')
        if '.' in text:
            text = text.rstrip('0').rstrip('.')
        return b'D' + (text if number else '0').encode()
    if isinstance(value, float):
        if math.isnan(value):
            return b'Fnan'
        return b'F' + value.hex().encode()
    if isinstance(value, dt.datetime):
        if value.tzinfo:
            value = value.astimezone(dt.timezone.utc)
        return b'T' + value.isoformat(timespec='microseconds').encode()
    if isinstance(value, dt.date):
        return b'T' + dt.datetime.combine(value, dt.time()).isoformat(timespec='microseconds').encode()
    if isinstance(value, str):
        return b'S' + value.encode('utf-8')
    raise TypeError('Unsupported verification value: ' + type(value).__name__)


def row_hash(row):
    digest = hashlib.sha256()
    for value in row:
        part = canonical(value)
        digest.update(len(part).to_bytes(8, 'big'))
        digest.update(part)
    return digest.digest()


def number_text(value):
    return None if value is None else canonical(value)[1:].decode()


def compare_cursors(source, target, width, batch=2000):
    """Store hash multiplicities on disk; never sort or hold a whole table in RAM."""
    with tempfile.TemporaryDirectory(prefix='moderniser-verify-') as tmp:
        db = sqlite3.connect(str(Path(tmp) / 'rows.sqlite'))
        try:
            db.execute('PRAGMA cache_size=-8192')
            db.execute('CREATE TABLE hashes(h BLOB PRIMARY KEY,n INTEGER NOT NULL) WITHOUT ROWID')
            stats = []
            for cursor, sign in ((source, 1), (target, -1)):
                count, nulls = 0, [0] * width
                totals = [decimal.Decimal(0)] * width
                minima, maxima = [None] * width, [None] * width
                with decimal.localcontext() as context:
                    context.prec = 100
                    while True:
                        rows = cursor.fetchmany(batch)
                        if not rows:
                            break
                        count += len(rows)
                        updates = []
                        for row in rows:
                            updates.append((row_hash(row), sign))
                            for i, value in enumerate(row):
                                if value is None:
                                    nulls[i] += 1
                                elif isinstance(value, (int, decimal.Decimal)):
                                    number = decimal.Decimal(value)
                                    totals[i] += number
                                    minima[i] = number if minima[i] is None else min(number, minima[i])
                                    maxima[i] = number if maxima[i] is None else max(number, maxima[i])
                        db.executemany('INSERT INTO hashes VALUES (?,?) ON CONFLICT(h) DO UPDATE SET n=n+excluded.n', updates)
                        db.commit()
                stats.append(dict(rows=count, nulls=nulls, numeric_totals=list(map(number_text, totals)),
                                  numeric_min=list(map(number_text, minima)), numeric_max=list(map(number_text, maxima))))
            mismatches = db.execute('SELECT coalesce(sum(abs(n)),0) FROM hashes WHERE n<>0').fetchone()[0]
            return dict(passed=mismatches == 0 and stats[0] == stats[1],
                        unmatched_rows=mismatches, source=stats[0], target=stats[1],
                        method='SHA-256 length-framed values; disk-backed multiset including duplicate multiplicities')
        finally:
            db.close()


def verify_structure(pg, mapped):
    """Compare actual definitions to the approved mapping in a rolled-back reference schema."""
    from write_postgres_schema import build_create_table_sql
    namespace = 'verify_' + uuid.uuid4().hex
    def read(cur, schema):
        cur.execute('''SELECT t.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
            pg_get_expr(d.adbin,d.adrelid)
            FROM pg_attribute a JOIN pg_class t ON t.oid=a.attrelid JOIN pg_namespace n ON n.oid=t.relnamespace
            LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE n.nspname=%s AND t.relkind='r' AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1,2''',(schema,))
        columns = [tuple(value.replace(namespace+'.','').replace('public.','') if isinstance(value,str) else value for value in row) for row in cur.fetchall()]
        cur.execute('''SELECT t.relname,c.contype,pg_get_constraintdef(c.oid,true),c.convalidated
            FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
            WHERE n.nspname=%s ORDER BY 1,2,3''',(schema,))
        constraints = sorted((t,k,re.sub(r'\b'+re.escape(namespace)+r'\.', '', definition).replace('public.',''),valid)
                             for t,k,definition,valid in cur.fetchall())
        cur.execute('''SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s ORDER BY indexname''',(schema,))
        indexes = [(name,definition.replace(namespace+'.','').replace('public.','')) for name,definition in cur.fetchall()]
        cur.execute('''SELECT viewname,definition FROM pg_views WHERE schemaname=%s ORDER BY viewname''',(schema,))
        views = [(name,definition.replace(namespace+'.','').replace('public.','')) for name,definition in cur.fetchall()]
        return columns,constraints,indexes,views
    pg.rollback()
    try:
        with pg.cursor() as cur:
            actual = read(cur,'public')
            cur.execute('CREATE SCHEMA "'+namespace+'"')
            cur.execute('SET LOCAL search_path TO "'+namespace+'"')
            for table in mapped['tables']:
                cur.execute(build_create_table_sql(table))
            from .objects import statements
            for statement in statements(mapped):
                cur.execute(statement)
            expected = read(cur,namespace)
            return dict(passed=actual==expected, columns_match=actual[0]==expected[0],
                        constraint_definitions_match=actual[1]==expected[1],
                        index_definitions_match=actual[2]==expected[2],view_definitions_match=actual[3]==expected[3],
                        scope='Approved types, nullability, PK, UNIQUE, CHECK, FK, ordinary index and supported view definitions')
    finally:
        pg.rollback()


def verify_database(mapped, database=None):
    import os
    import oracledb
    import psycopg2
    from migrate_data import get_oracle_connection, get_pg_connection
    from config import ORACLE_SCHEMA, PG_HOST, PG_PORT, PG_USER, PG_PASSWORD
    # Oracle NUMBER must be read as Decimal, never through a binary float.
    oracledb.defaults.fetch_decimals = True
    oracle = get_oracle_connection()
    pg = get_pg_connection() if database is None else psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=database)
    results = {}
    try:
        with oracle.cursor() as source:
            source.execute("ALTER SESSION SET TIME_ZONE='+00:00'")
            for table in mapped['tables']:
                columns = ','.join('"' + c['name'].replace('"', '""') + '"' for c in table['columns'])
                source_columns = ','.join('"' + c['name'].upper().replace('"', '""') + '"' for c in table['columns'])
                name = table['name'].replace('"', '""')
                snapshot = os.getenv('ORACLE_SNAPSHOT_SCN', '')
                flashback = ' AS OF SCN ' + str(int(snapshot)) if snapshot else ''
                source.execute(f'SELECT {source_columns} FROM "{ORACLE_SCHEMA}"."{name.upper()}"{flashback}')
                with pg.cursor(name='verify_' + str(len(results))) as target:
                    target.execute(f'SELECT {columns} FROM "{name}"')
                    results[table['name']] = compare_cursors(source, target, len(table['columns']))
        structure = verify_structure(pg, mapped)
        return dict(passed=all(t['passed'] for t in results.values()) and structure['passed'], tables=results, structure=structure)
    finally:
        oracle.close()
        pg.close()
