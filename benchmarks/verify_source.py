"""Validate the pinned independent source without modifying any source rows."""
import json,os
import oracledb
from pathlib import Path
from moderniser.provision import grant_reader
EXPECTED={'CHANNELS':5,'COSTS':82112,'COUNTRIES':35,'CUSTOMERS':55500,'PRODUCTS':72,'PROMOTIONS':503,'SALES':918843,'TIMES':1826,'SUPPLEMENTARY_DEMOGRAPHICS':4500}
with oracledb.connect(user='sys',password=os.environ['ORACLE_PASSWORD'],dsn=os.environ['ORACLE_DSN'],mode=oracledb.AUTH_MODE_SYSDBA) as conn,conn.cursor() as cur:
 cur.execute("SELECT count(*) FROM dba_users WHERE username='SH'")
 if not cur.fetchone()[0]:raise SystemExit(3)
 counts={}
 for table,expected in EXPECTED.items():
  cur.execute('SELECT count(*) FROM SH.'+table);counts[table]=cur.fetchone()[0]
  assert counts[table]==expected,(table,counts[table],expected)
 cur.execute("SELECT sum(bytes) FROM dba_segments WHERE owner='SH'");size=cur.fetchone()[0]
report={'commit':'6660bad68c07bd143430ace58565b3f727e17263','counts':counts,'total_rows':sum(counts.values()),'source_segment_bytes':size,'adaptations':['Interactive ACCEPT prompts supplied as DEFINE values.','Oracle Text index SUP_TEXT_IDX omitted on the slim image; base data unchanged.'],'base_table_counts_passed':True}
print(json.dumps(report,indent=2))
if Path('/bench').exists():Path('/bench/installation.json').write_text(json.dumps(report,indent=2))
grant_reader()
