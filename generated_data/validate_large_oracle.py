"""Validate the fixture in a temporary Oracle schema, then remove that schema."""
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
import oracledb
from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN
from upload_pipeline import create_scratch_schema, drop_scratch_schema, run_script

def main():
    manifest=json.loads((ROOT/'large_oracle_store_manifest.json').read_text())
    schema='UPL_'+secrets.token_hex(4).upper()
    password=secrets.token_hex(12)
    admin=oracledb.connect(user=ORACLE_USER,password=ORACLE_PASSWORD,dsn=ORACLE_DSN)
    conn=None
    created=False
    try:
        create_scratch_schema(admin,schema,password)
        created=True
        conn=oracledb.connect(user=schema,password=password,dsn=ORACLE_DSN)
        text=(ROOT/manifest['file']).read_text(encoding='utf-8')
        result=run_script(conn,text)
        print(f'Statements executed: {result.executed}; failed: {len(result.failed)}; compile errors: {len(result.compile_errors)}',flush=True)
        if result.failed or result.compile_errors:
            raise RuntimeError(str(result.failed[:5])+str(result.compile_errors[:5]))
        cur=conn.cursor()
        counts={}
        for table,expected in manifest['tables'].items():
            cur.execute(f'SELECT COUNT(*) FROM {table}')
            actual=cur.fetchone()[0]
            if actual!=expected:
                raise RuntimeError(f'{table}: expected {expected}, found {actual}')
            counts[table]=actual
        checks={
          'order_totals': '''SELECT COUNT(*) FROM orders o JOIN
              (SELECT order_id,SUM(quantity*unit_price) amount FROM order_items GROUP BY order_id) i
              ON o.id=i.order_id WHERE o.total_amount != i.amount''',
          'payment_totals': 'SELECT COUNT(*) FROM payments p JOIN orders o ON o.id=p.order_id WHERE p.amount != o.total_amount',
          'address_owners': 'SELECT COUNT(*) FROM orders o JOIN addresses a ON o.address_id=a.id WHERE o.customer_id != a.customer_id',
          'payment_status': "SELECT COUNT(*) FROM payments p JOIN orders o ON o.id=p.order_id WHERE o.status NOT IN ('PAID','SHIPPED','DELIVERED')",
          'shipment_status': "SELECT COUNT(*) FROM shipments s JOIN orders o ON o.id=s.order_id WHERE o.status NOT IN ('SHIPPED','DELIVERED')"
        }
        for name,sql in checks.items():
            cur.execute(sql)
            if cur.fetchone()[0]!=0:
                raise RuntimeError(f'Failed business consistency check: {name}')
        cur.execute('SELECT customer_order_count(1),calculated_order_total(1),low_stock_count(200) FROM dual')
        outputs=cur.fetchone()
        if outputs[0]!=3:
            raise RuntimeError('Customer order count routine returned an unexpected result')
        cur.execute('SELECT total_amount FROM orders WHERE id=1')
        if outputs[1]!=cur.fetchone()[0]:
            raise RuntimeError('Order total routine returned an unexpected result')
        cur.execute('SELECT stock FROM products WHERE id=1')
        stock=cur.fetchone()[0]
        cur.callproc('adjust_product_stock',[1,5])
        cur.execute('SELECT stock FROM products WHERE id=1')
        if cur.fetchone()[0]!=stock+5:
            raise RuntimeError('Stock adjustment routine returned an unexpected result')
        conn.rollback()
        cur.close()
        report=dict(status='PASS',database='Oracle',tables=counts,total_rows=sum(counts.values()),
                    statements_executed=result.executed,failed_statements=0,compile_errors=0,
                    business_checks=list(checks),routines_executed=4,
                    scope='Oracle source fixture validation; PostgreSQL migration and AI translation not run')
        (ROOT/'large_oracle_store_validation.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(report,indent=2),flush=True)
    finally:
        if conn:
            conn.close()
        try:
            if created:
                drop_scratch_schema(admin,schema)
                print('Temporary Oracle validation schema removed.',flush=True)
        finally:
            admin.close()

if __name__=='__main__':
    main()
