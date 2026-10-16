"""Differential checker acceptance on disposable Oracle/PostgreSQL copies (no AI claims)."""
import json,os,secrets,uuid
from dataclasses import asdict
from pathlib import Path
import oracledb,psycopg2
import upload_pipeline as u
jid=uuid.uuid4().hex;schema='UPL_'+jid[:24].upper();database='upload_'+jid
admin=oracledb.connect(user='sys',password=os.environ['ORACLE_PASSWORD'],dsn=os.environ['ORACLE_DSN'],mode=oracledb.AUTH_MODE_SYSDBA)
password=secrets.token_hex(12)
u.create_scratch_schema(admin,schema,password)
os.environ.update(ORACLE_USER=schema,ORACLE_SCHEMA=schema,ORACLE_PASSWORD=password,PG_DBNAME=database)
u.create_pg_database(database)
from migrate_data import get_oracle_connection,get_pg_connection
from read_schema import read_source_objects
from compare_procedures import run_cases,choose_oracle_runner,PostgresRunner,format_report
source="""CREATE TABLE items(id NUMBER(9) PRIMARY KEY,qty NUMBER(9));
INSERT INTO items VALUES(1,5);
CREATE OR REPLACE FUNCTION twice(x NUMBER) RETURN NUMBER IS BEGIN RETURN x*2; END;
/
CREATE OR REPLACE PROCEDURE increase_quantity(p_id NUMBER,p_qty NUMBER) IS
BEGIN
 IF p_qty<=0 THEN RAISE_APPLICATION_ERROR(-20001,'Quantity must be positive'); END IF;
 UPDATE items SET qty=qty+p_qty WHERE id=p_id;
END;
/
COMMIT;
"""
try:
 with get_oracle_connection() as c:u.run_script(c,source)
 with get_pg_connection() as c,c.cursor() as cur:
  cur.execute("""CREATE TABLE items(id INTEGER PRIMARY KEY,qty INTEGER);INSERT INTO items VALUES(1,5);
CREATE FUNCTION twice(x NUMERIC) RETURNS NUMERIC LANGUAGE plpgsql AS $$BEGIN RETURN x*2;END$$;
CREATE FUNCTION increase_quantity(p_id NUMERIC,p_qty NUMERIC) RETURNS VOID LANGUAGE plpgsql AS $$BEGIN
 IF p_qty<=0 THEN RAISE EXCEPTION 'Quantity must be positive';END IF;
 UPDATE items SET qty=qty+p_qty WHERE id=p_id;END$$;""");c.commit()
 cases=[{'call':'twice','args':[v],'returns':'number'} for v in (0,1,-2,0.1,None)]
 cases.extend([{'call':'increase_quantity','args':[1,2],'observe':['SELECT id,qty FROM items ORDER BY id']},{'call':'increase_quantity','args':[1,0]}])
 sources={o['name'].upper():o['source'] for o in read_source_objects()}
 with get_oracle_connection() as source,get_pg_connection() as target:
  results=run_cases(cases,choose_oracle_runner(False,source,None,get_oracle_connection),PostgresRunner(target),sources,False,0)
 print(format_report(results))
 assert all(r.status=='SAME' for r in results)
 result={'cases':[asdict(r) for r in results],'passed':True,'scope':'Checker verified against explicitly equivalent fixtures; does not certify AI translation.'}
 Path('/app/logs/procedure-acceptance.json').write_text(json.dumps(result,indent=2,default=str))
finally:
 u.drop_scratch_schema(admin,schema);admin.close();u.drop_pg_database(database)
